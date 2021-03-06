from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import argparse
import logging
import os
import time
import traceback

import redis

import ray
from ray.autoscaler.autoscaler import LoadMetrics, StandardAutoscaler
import ray.cloudpickle as pickle
import ray.gcs_utils
import ray.utils
import ray.ray_constants as ray_constants
from ray.utils import (binary_to_hex, binary_to_object_id, binary_to_task_id,
                       hex_to_binary, setup_logger)

logger = logging.getLogger(__name__)


class Monitor(object):
    """A monitor for Ray processes.

    The monitor is in charge of cleaning up the tables in the global state
    after processes have died. The monitor is currently not responsible for
    detecting component failures.

    Attributes:
        redis: A connection to the Redis server.
        primary_subscribe_client: A pubsub client for the Redis server.
            This is used to receive notifications about failed components.
    """

    def __init__(self, redis_address, autoscaling_config, redis_password=None):
        # Initialize the Redis clients.
        ray.state.state._initialize_global_state(
            args.redis_address, redis_password=redis_password)
        self.redis = ray.services.create_redis_client(
            redis_address, password=redis_password)
        # Setup subscriptions to the primary Redis server and the Redis shards.
        self.primary_subscribe_client = self.redis.pubsub(
            ignore_subscribe_messages=True)
        # Keep a mapping from raylet client ID to IP address to use
        # for updating the load metrics.
        self.raylet_id_to_ip_map = {}
        self.load_metrics = LoadMetrics()
        if autoscaling_config:
            self.autoscaler = StandardAutoscaler(autoscaling_config,
                                                 self.load_metrics)
        else:
            self.autoscaler = None

        # Experimental feature: GCS flushing.
        self.issue_gcs_flushes = "RAY_USE_NEW_GCS" in os.environ
        self.gcs_flush_policy = None
        if self.issue_gcs_flushes:
            # Data is stored under the first data shard, so we issue flushes to
            # that redis server.
            addr_port = self.redis.lrange("RedisShards", 0, -1)
            if len(addr_port) > 1:
                logger.warning(
                    "Monitor: "
                    "TODO: if launching > 1 redis shard, flushing needs to "
                    "touch shards in parallel.")
                self.issue_gcs_flushes = False
            else:
                addr_port = addr_port[0].split(b":")
                self.redis_shard = redis.StrictRedis(
                    host=addr_port[0],
                    port=addr_port[1],
                    password=redis_password)
                try:
                    self.redis_shard.execute_command("HEAD.FLUSH 0")
                except redis.exceptions.ResponseError as e:
                    logger.info(
                        "Monitor: "
                        "Turning off flushing due to exception: {}".format(
                            str(e)))
                    self.issue_gcs_flushes = False

    def __del__(self):
        """Destruct the monitor object."""
        # We close the pubsub client to avoid leaking file descriptors.
        self.primary_subscribe_client.close()

    def subscribe(self, channel):
        """Subscribe to the given channel on the primary Redis shard.

        Args:
            channel (str): The channel to subscribe to.

        Raises:
            Exception: An exception is raised if the subscription fails.
        """
        self.primary_subscribe_client.subscribe(channel)

    def xray_heartbeat_batch_handler(self, unused_channel, data):
        """Handle an xray heartbeat batch message from Redis."""

        gcs_entries = ray.gcs_utils.GcsEntry.FromString(data)
        heartbeat_data = gcs_entries.entries[0]

        message = ray.gcs_utils.HeartbeatBatchTableData.FromString(
            heartbeat_data)

        for heartbeat_message in message.batch:
            num_resources = len(heartbeat_message.resources_available_label)
            static_resources = {}
            dynamic_resources = {}
            for i in range(num_resources):
                dyn = heartbeat_message.resources_available_label[i]
                static = heartbeat_message.resources_total_label[i]
                dynamic_resources[dyn] = (
                    heartbeat_message.resources_available_capacity[i])
                static_resources[static] = (
                    heartbeat_message.resources_total_capacity[i])

            # Update the load metrics for this raylet.
            client_id = ray.utils.binary_to_hex(heartbeat_message.client_id)
            ip = self.raylet_id_to_ip_map.get(client_id)
            if ip:
                self.load_metrics.update(ip, static_resources,
                                         dynamic_resources)
            else:
                logger.warning(
                    "Monitor: "
                    "could not find ip for client {}".format(client_id))

    def _xray_clean_up_entries_for_job(self, job_id):
        """Remove this job's object/task entries from redis.

        Removes control-state entries of all tasks and task return
        objects belonging to the driver.

        Args:
            job_id: The job id.
        """

        xray_task_table_prefix = (
            ray.gcs_utils.TablePrefix_RAYLET_TASK_string.encode("ascii"))
        xray_object_table_prefix = (
            ray.gcs_utils.TablePrefix_OBJECT_string.encode("ascii"))

        task_table_objects = ray.tasks()
        job_id_hex = binary_to_hex(job_id)
        job_task_id_bins = set()
        for task_id_hex, task_info in task_table_objects.items():
            task_table_object = task_info["TaskSpec"]
            task_job_id_hex = task_table_object["JobID"]
            if job_id_hex != task_job_id_hex:
                # Ignore tasks that aren't from this driver.
                continue
            job_task_id_bins.add(hex_to_binary(task_id_hex))

        # Get objects associated with the driver.
        object_table_objects = ray.objects()
        job_object_id_bins = set()
        for object_id, _ in object_table_objects.items():
            task_id_bin = ray._raylet.compute_task_id(object_id).binary()
            if task_id_bin in job_task_id_bins:
                job_object_id_bins.add(object_id.binary())

        def to_shard_index(id_bin):
            if len(id_bin) == ray.TaskID.size():
                return binary_to_task_id(id_bin).redis_shard_hash() % len(
                    ray.state.state.redis_clients)
            else:
                return binary_to_object_id(id_bin).redis_shard_hash() % len(
                    ray.state.state.redis_clients)

        # Form the redis keys to delete.
        sharded_keys = [[] for _ in range(len(ray.state.state.redis_clients))]
        for task_id_bin in job_task_id_bins:
            sharded_keys[to_shard_index(task_id_bin)].append(
                xray_task_table_prefix + task_id_bin)
        for object_id_bin in job_object_id_bins:
            sharded_keys[to_shard_index(object_id_bin)].append(
                xray_object_table_prefix + object_id_bin)

        # Remove with best effort.
        for shard_index in range(len(sharded_keys)):
            keys = sharded_keys[shard_index]
            if len(keys) == 0:
                continue
            redis = ray.state.state.redis_clients[shard_index]
            num_deleted = redis.delete(*keys)
            logger.info("Monitor: "
                        "Removed {} dead redis entries of the "
                        "driver from redis shard {}.".format(
                            num_deleted, shard_index))
            if num_deleted != len(keys):
                logger.warning("Monitor: "
                               "Failed to remove {} relevant redis "
                               "entries from redis shard {}.".format(
                                   len(keys) - num_deleted, shard_index))

    def xray_job_removed_handler(self, unused_channel, data):
        """Handle a notification that a job has been removed.

        Args:
            unused_channel: The message channel.
            data: The message data.
        """
        gcs_entries = ray.gcs_utils.GcsEntry.FromString(data)
        job_data = gcs_entries.entries[0]
        message = ray.gcs_utils.JobTableData.FromString(job_data)
        job_id = message.job_id
        logger.info("Monitor: "
                    "XRay Driver {} has been removed.".format(
                        binary_to_hex(job_id)))
        self._xray_clean_up_entries_for_job(job_id)

    def process_messages(self, max_messages=10000):
        """Process all messages ready in the subscription channels.

        This reads messages from the subscription channels and calls the
        appropriate handlers until there are no messages left.

        Args:
            max_messages: The maximum number of messages to process before
                returning.
        """
        subscribe_clients = [self.primary_subscribe_client]
        for subscribe_client in subscribe_clients:
            for _ in range(max_messages):
                message = subscribe_client.get_message()
                if message is None:
                    # Continue on to the next subscribe client.
                    break

                # Parse the message.
                channel = message["channel"]
                data = message["data"]

                # Determine the appropriate message handler.
                if channel == ray.gcs_utils.XRAY_HEARTBEAT_BATCH_CHANNEL:
                    # Similar functionality as raylet info channel
                    message_handler = self.xray_heartbeat_batch_handler
                elif channel == ray.gcs_utils.XRAY_JOB_CHANNEL:
                    # Handles driver death.
                    message_handler = self.xray_job_removed_handler
                else:
                    raise Exception("This code should be unreachable.")

                # Call the handler.
                message_handler(channel, data)

    def update_raylet_map(self):
        all_raylet_nodes = ray.nodes()
        self.raylet_id_to_ip_map = {}
        for raylet_info in all_raylet_nodes:
            client_id = (raylet_info.get("DBClientID")
                         or raylet_info["ClientID"])
            ip_address = (raylet_info.get("AuxAddress")
                          or raylet_info["NodeManagerAddress"]).split(":")[0]
            self.raylet_id_to_ip_map[client_id] = ip_address

    def _maybe_flush_gcs(self):
        """Experimental: issue a flush request to the GCS.

        The purpose of this feature is to control GCS memory usage.

        To activate this feature, Ray must be compiled with the flag
        RAY_USE_NEW_GCS set, and Ray must be started at run time with the flag
        as well.
        """
        if not self.issue_gcs_flushes:
            return
        if self.gcs_flush_policy is None:
            serialized = self.redis.get("gcs_flushing_policy")
            if serialized is None:
                # Client has not set any policy; by default flushing is off.
                return
            self.gcs_flush_policy = pickle.loads(serialized)

        if not self.gcs_flush_policy.should_flush(self.redis_shard):
            return

        max_entries_to_flush = self.gcs_flush_policy.num_entries_to_flush()
        num_flushed = self.redis_shard.execute_command(
            "HEAD.FLUSH {}".format(max_entries_to_flush))
        logger.info("Monitor: num_flushed {}".format(num_flushed))

        # This flushes event log and log files.
        ray.experimental.flush_redis_unsafe(self.redis)

        self.gcs_flush_policy.record_flush()

    def _run(self):
        """Run the monitor.

        This function loops forever, checking for messages about dead database
        clients and cleaning up state accordingly.
        """
        # Initialize the subscription channel.
        self.subscribe(ray.gcs_utils.XRAY_HEARTBEAT_BATCH_CHANNEL)
        self.subscribe(ray.gcs_utils.XRAY_JOB_CHANNEL)

        # TODO(rkn): If there were any dead clients at startup, we should clean
        # up the associated state in the state tables.

        # Handle messages from the subscription channels.
        while True:
            # Update the mapping from raylet client ID to IP address.
            # This is only used to update the load metrics for the autoscaler.
            self.update_raylet_map()

            # Process autoscaling actions
            if self.autoscaler:
                self.autoscaler.update()

            self._maybe_flush_gcs()

            # Process a round of messages.
            self.process_messages()

            # Wait for a heartbeat interval before processing the next round of
            # messages.
            time.sleep(ray._config.heartbeat_timeout_milliseconds() * 1e-3)

    def run(self):
        try:
            self._run()
        except Exception:
            if self.autoscaler:
                self.autoscaler.kill_workers()
            raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=("Parse Redis server for the "
                     "monitor to connect to."))
    parser.add_argument(
        "--redis-address",
        required=True,
        type=str,
        help="the address to use for Redis")
    parser.add_argument(
        "--autoscaling-config",
        required=False,
        type=str,
        help="the path to the autoscaling config file")
    parser.add_argument(
        "--redis-password",
        required=False,
        type=str,
        default=None,
        help="the password to use for Redis")
    parser.add_argument(
        "--logging-level",
        required=False,
        type=str,
        default=ray_constants.LOGGER_LEVEL,
        choices=ray_constants.LOGGER_LEVEL_CHOICES,
        help=ray_constants.LOGGER_LEVEL_HELP)
    parser.add_argument(
        "--logging-format",
        required=False,
        type=str,
        default=ray_constants.LOGGER_FORMAT,
        help=ray_constants.LOGGER_FORMAT_HELP)
    args = parser.parse_args()
    setup_logger(args.logging_level, args.logging_format)

    if args.autoscaling_config:
        autoscaling_config = os.path.expanduser(args.autoscaling_config)
    else:
        autoscaling_config = None

    monitor = Monitor(
        args.redis_address,
        autoscaling_config,
        redis_password=args.redis_password)

    try:
        monitor.run()
    except Exception as e:
        # Something went wrong, so push an error to all drivers.
        redis_client = ray.services.create_redis_client(
            args.redis_address, password=args.redis_password)
        traceback_str = ray.utils.format_error_message(traceback.format_exc())
        message = "The monitor failed with the following error:\n{}".format(
            traceback_str)
        ray.utils.push_error_to_driver_through_redis(
            redis_client, ray_constants.MONITOR_DIED_ERROR, message)
        raise e
