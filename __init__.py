# -*- coding: utf-8 -*-
#
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
#

"""
This module contains operators to replicate records from
DynamoDB table to S3.
"""

import gzip
import shutil
from copy import copy
from io import BytesIO
from os.path import getsize
from tempfile import NamedTemporaryFile
from typing import Any, Callable, Dict, Optional
from uuid import uuid4

from airflow.contrib.hooks.aws_dynamodb_hook import AwsDynamoDBHook
from airflow.hooks.S3_hook import S3Hook
from airflow.models.baseoperator import BaseOperator
from boto.compat import json  # type: ignore


def _convert_item_to_json_bytes(item):
    return (json.dumps(item) + '\n').encode('utf-8')


def _upload_file_to_s3(file_obj, bucket_name, s3_key_prefix, aws_conn_id):
    s3_client = S3Hook(aws_conn_id=aws_conn_id).get_conn()
    file_obj.seek(0)

    compressed_file_obj = BytesIO()
    with gzip.GzipFile(fileobj=compressed_file_obj, mode='wb') as gz:
        shutil.copyfileobj(file_obj, gz)

    compressed_file_obj.seek(0)
    key = s3_key_prefix + str(uuid4()) + '.gz'
    s3_client.upload_fileobj(compressed_file_obj, bucket_name, key)


class DynamoDBToS3OperatorV2(BaseOperator):
    """
    Replicates records from a DynamoDB table to S3.
    It scans a DynamoDB table and write the received records to a file
    on the local filesystem. It flushes the file to S3 once the file size
    exceeds the file size limit specified by the user.

    Users can also specify a filtering criteria using dynamodb_scan_kwargs
    to only replicate records that satisfy the criteria.

    To parallelize the replication, users can create multiple tasks of DynamoDBToS3Operator.
    For instance to replicate with parallelism of 2, create two tasks like:

    .. code-block::

        op1 = DynamoDBToS3Operator(
            task_id='replicator-1',
            dynamodb_table_name='hello',
            dynamodb_scan_kwargs={
                'TotalSegments': 2,
                'Segment': 0,
            },
            ...
        )

        op2 = DynamoDBToS3Operator(
            task_id='replicator-2',
            dynamodb_table_name='hello',
            dynamodb_scan_kwargs={
                'TotalSegments': 2,
                'Segment': 1,
            },
            ...
        )

    :param dynamodb_table_name: Dynamodb table to replicate data from
    :param s3_bucket_name: S3 bucket to replicate data to
    :param file_size: Flush file to s3 if file size >= file_size
    :param dynamodb_scan_kwargs: kwargs pass to <https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/dynamodb.html#DynamoDB.Table.scan>  # noqa: E501 pylint: disable=line-too-long
    :param s3_key_prefix: Prefix of s3 object key
    :param process_func: How we transforms a dynamodb item to bytes. By default we dump the json
    """

    def __init__(self,
                 dynamodb_table_name,  # type: str
                 s3_bucket_name,  # type: str
                 file_size,  # ty: int
                 dynamodb_scan_kwargs=None,  # type: Optional[Dict[str, Any]]
                 s3_key_prefix='',  # type: str
                 process_func=_convert_item_to_json_bytes,  # type: Callable[[Dict[str, Any]], bytes]
                 aws_conn_id='aws_default',
                 *args, **kwargs):
        super(DynamoDBToS3OperatorV2, self).__init__(*args, **kwargs)
        self.file_size = file_size
        self.process_func = process_func
        self.dynamodb_table_name = dynamodb_table_name
        self.dynamodb_scan_kwargs = dynamodb_scan_kwargs
        self.s3_bucket_name = s3_bucket_name
        self.s3_key_prefix = s3_key_prefix
        self.aws_conn_id = aws_conn_id

    def execute(self, context):
        table = AwsDynamoDBHook(aws_conn_id=self.aws_conn_id).get_conn().Table(self.dynamodb_table_name)
        scan_kwargs = copy(self.dynamodb_scan_kwargs) if self.dynamodb_scan_kwargs else {}
        err = None
        f = NamedTemporaryFile()
        try:
            f = self._scan_dynamodb_and_upload_to_s3(f, scan_kwargs, table)
        except Exception as e:
            err = e
            raise e
        finally:
            if err is None:
                _upload_file_to_s3(f, self.s3_bucket_name, self.s3_key_prefix, self.aws_conn_id)
            f.close()

    def _scan_dynamodb_and_upload_to_s3(self, temp_file, scan_kwargs, table):
        while True:
            response = table.scan(**scan_kwargs)
            items = response['Items']
            for item in items:
                temp_file.write(self.process_func(item))

            if 'LastEvaluatedKey' not in response:
                # no more items to scan
                break

            last_evaluated_key = response['LastEvaluatedKey']
            scan_kwargs['ExclusiveStartKey'] = last_evaluated_key

            # Upload the file to S3 if reach file size limit
            if getsize(temp_file.name) >= self.file_size:
                _upload_file_to_s3(temp_file, self.s3_bucket_name,
                                   self.s3_key_prefix, self.aws_conn_id)
                temp_file.close()
                temp_file = NamedTemporaryFile()
        return temp_file
