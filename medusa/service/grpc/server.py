# -*- coding: utf-8 -*-
# Copyright 2020- Datastax, Inc. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import asyncio
import logging
import signal
import sys
from collections import defaultdict
from concurrent import futures
from concurrent.futures.thread import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import grpc
import grpc_health.v1.health
from grpc_health.v1 import health_pb2_grpc

from medusa import backup_node
from medusa.config import load_config
from medusa.listing import get_backups
from medusa.purge import delete_backup
from medusa.service.grpc import medusa_pb2
from medusa.service.grpc import medusa_pb2_grpc
from medusa.storage import Storage
from medusa.backup_manager import BackupMan

TIMESTAMP_FORMAT = '%Y-%m-%d %H:%M:%S'
BACKUP_MODE_DIFFERENTIAL = "differential"
BACKUP_MODE_FULL = "full"


class Server:
    def __init__(self, config_file_path, testing=False):
        self.config_file_path = config_file_path
        self.medusa_config = self.create_config()
        self.testing = testing
        self.grpc_server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
        logging.info("GRPC server initialized")

    def shutdown(self, signum, frame):
        logging.info("Shutting down GRPC server")
        handle_backup_removal_all()
        self.grpc_server.stop(0)

    def serve(self):
        config = self.create_config()
        self.configure_console_logging()

        medusa_pb2_grpc.add_MedusaServicer_to_server(MedusaService(config), self.grpc_server)
        health_pb2_grpc.add_HealthServicer_to_server(grpc_health.v1.health.HealthServicer(), self.grpc_server)

        logging.info('Starting server. Listening on port 50051.')
        self.grpc_server.add_insecure_port('[::]:50051')
        self.grpc_server.start()

        if not self.testing:
            signal.signal(signal.SIGTERM, self.shutdown)
            self.grpc_server.wait_for_termination()

    def create_config(self):
        config_file = Path(self.config_file_path)
        args = defaultdict(lambda: None)

        return load_config(args, config_file)

    def configure_console_logging(self):
        root_logger = logging.getLogger('')
        root_logger.setLevel(logging.DEBUG)

        log_format = logging.Formatter('[%(asctime)s] %(levelname)s: %(message)s')

        console_handler = logging.StreamHandler()
        console_handler.setLevel(getattr(logging, self.medusa_config.logging.level))
        console_handler.setFormatter(log_format)
        root_logger.addHandler(console_handler)

        if console_handler.level > logging.DEBUG:
            # Disable debugging logging for external libraries
            for logger_name in 'urllib3', 'google_cloud_storage.auth.transport.requests', 'paramiko', 'cassandra':
                logging.getLogger(logger_name).setLevel(logging.WARN)


class MedusaService(medusa_pb2_grpc.MedusaServicer):

    def __init__(self, config):
        logging.info("Init service")
        self.config = config
        self.storage = Storage(config=self.config.storage)

    def Backup(self, request, context):
        logging.info("Performing backup {} (type={})".format(request.name, request.mode))
        resp = medusa_pb2.BackupResponse()
        # TODO pass the staggered arg
        mode = BACKUP_MODE_DIFFERENTIAL
        if medusa_pb2.BackupRequest.Mode.FULL == request.mode:
            mode = BACKUP_MODE_FULL
        try:
            resp.backupName = request.name
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix=request.name) as executor:
                backup_future = executor.submit(backup_node.handle_backup, config=self.config,
                                                backup_name_arg=request.name, stagger_time=None,
                                                enable_md5_checks_flag=False, mode=mode)
                BackupMan.set_backup(resp.backupName, backup_future)

            resp.status = medusa_pb2.BackupStatusType.IN_PROGRESS
            logging.info("Backup {} is in progress".format(request.name))
        except Exception as e:
            context.set_details("failed to create backups: {}".format(e))
            context.set_code(grpc.StatusCode.INTERNAL)
            logging.exception("backup failed")
            resp.status = medusa_pb2.BackupStatusType.FAILED

        return resp

    def BackupStatus(self, request, context):
        response = medusa_pb2.BackupStatusResponse()
        try:
            backup = self.storage.get_cluster_backup(request.backupName)
            logging.info("Checking status: handle_backup_status ")
            status, _ = handle_backup_status(request.backupName)

            # TODO how is the startTime determined?
            response.startTime = datetime.fromtimestamp(backup.started).strftime(TIMESTAMP_FORMAT)
            response.finishedNodes = [node.fqdn for node in backup.complete_nodes()]
            response.unfinishedNodes = [node.fqdn for node in backup.incomplete_nodes()]
            response.missingNodes = [node.fqdn for node in backup.missing_nodes()]
            if backup.finished:
                response.finishTime = datetime.fromtimestamp(backup.finished).strftime(TIMESTAMP_FORMAT)
            else:
                response.finishTime = ""

            if status == BackupMan.IN_PROGRESS:
                response.status = medusa_pb2.BackupStatusType.IN_PROGRESS
            if status == BackupMan.FAILED:
                response.status = medusa_pb2.BackupStatusType.FAILED
            if status == BackupMan.SUCCESS:
                response.status = medusa_pb2.BackupStatusType.SUCCESS

            return response
        except KeyError:
            context.set_details("backup <{}> does not exist".format(request.backupName))
            context.set_code(grpc.StatusCode.NOT_FOUND)
            return response

    def GetBackups(self, request, context):
        response = medusa_pb2.GetBackupsResponse()
        last_status = BackupMan.SUCCESS
        try:
            # cluster backups
            backups = get_backups(self.config, True)
            for backup in backups:
                summary = medusa_pb2.BackupSummary()
                summary.backupName = backup.name

                if backup.started is None:
                    summary.startTime = 0
                else:
                    summary.startTime = backup.started

                if backup.finished is None:
                    summary.finishTime = 0
                    summary.status = BackupMan.IN_PROGRESS
                    last_status = BackupMan.IN_PROGRESS
                else:
                    summary.finishTime = backup.finished
                    if last_status != BackupMan.IN_PROGRESS:
                        summary.status = BackupMan.SUCCESS

                summary.totalNodes = len(backup.tokenmap)
                summary.finishedNodes = len(backup.complete_nodes())
                for node in backup.tokenmap:
                    tokenmap_node = medusa_pb2.BackupNode()
                    tokenmap_node.host = node
                    tokenmap_node.datacenter = backup.tokenmap[node]["dc"] if "dc" in backup.tokenmap[node] else ""
                    tokenmap_node.rack = backup.tokenmap[node]["rack"] if "rack" in backup.tokenmap[node] else ""
                    if "tokens" in backup.tokenmap[node]:
                        for token in backup.tokenmap[node]["tokens"]:
                            tokenmap_node.tokens.append(token)
                    summary.nodes.append(tokenmap_node)

                response.backups.append(summary)

            return response
        except Exception as e:
            context.set_details("failed to get backups: {}".format(e))
            context.set_code(grpc.StatusCode.INTERNAL)
            return response

    def DeleteBackup(self, request, context):
        logging.info("Deleting backup {}".format(request.name))
        resp = medusa_pb2.DeleteBackupResponse()
        try:
            delete_backup(self.config, [request.name], True)
            handle_backup_removal(request.name)
        except Exception as e:
            context.set_details("deleting backups failed: {}".format(e))
            context.set_code(grpc.StatusCode.INTERNAL)
            logging.exception("Deleting backup {} failed".format(request.name))

        return resp


# Returns the status of a registered backup along with set result if done.
# Returns None if not found.
def handle_backup_status(backup_name):
    backup_future = BackupMan.get_backup_future(backup_name)
    if backup_future and asyncio.isfuture(backup_future):
        if backup_future.done() and not backup_future.cancelled():
            result = backup_future.result()
            return BackupMan.SUCCESS, result
        elif not backup_future.done() and not backup_future.cancelled():
            return BackupMan.IN_PROGRESS, None
        else:
            return BackupMan.FAILED, None
    else:
        return None, None


def handle_backup_removal(backup_name):
    BackupMan.remove_backup(backup_name)


def handle_backup_removal_all():
    BackupMan.cleanup_all_backups()


if __name__ == '__main__':
    if len(sys.argv) > 2:
        config_file_path = sys.argv[2]
    else:
        config_file_path = "/etc/medusa/medusa.ini"

    server = Server(config_file_path)
    server.serve()
