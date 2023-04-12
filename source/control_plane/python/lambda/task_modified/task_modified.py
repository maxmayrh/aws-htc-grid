from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from utils.state_table_common import TASK_STATE_PENDING, TASK_STATE_WAITING, DEPENDENCY_STATE_FINISHED
import time
import copy
import os
import boto3
import json

from api.queue_manager import queue_manager
from api.state_table_manager import state_table_manager

region = os.environ["REGION"]

sqs = boto3.resource('sqs', endpoint_url=f'https://sqs.{region}.amazonaws.com')

tasks_queue = queue_manager(
    task_queue_service=os.environ['TASK_QUEUE_SERVICE'],
    task_queue_config=os.environ['TASK_QUEUE_CONFIG'],
    tasks_queue_name=os.environ['TASKS_QUEUE_NAME'],
    region=region)

state_table = state_table_manager(
    os.environ['STATE_TABLE_SERVICE'],
    os.environ['STATE_TABLE_CONFIG'],
    os.environ['STATE_TABLE_NAME'],
    os.environ["REGION"])

def to_ddb_types(dict):
    serializer = TypeSerializer()
    return {k: serializer.serialize(v) for k, v in dict.items()}

def from_ddb_types(dict):
    serializer = TypeDeserializer()
    return {k: serializer.deserialize(v) for k, v in dict.items()}

def state_changed(new_image, old_image, attribute, from_state=None, target_state=None):
    compare_target = True if target_state is None else new_image[attribute] == target_state
    if old_image is None:
        return compare_target if from_state is None else False
    else:
        compare_from = True if from_state is None else old_image[attribute] == from_state
        return compare_target and compare_from and \
            (new_image[attribute] != old_image[attribute])

def get_time_now_ms():
    return int(round(time.time() * 1000))

def write_to_sqs(sqs_batch_entries, session_priority=0):
    try:
        response = tasks_queue.send_messages(
            message_bodies=sqs_batch_entries,
            message_attributes={
                "priority": session_priority
            }
        )
        if response.get('Failed') is not None:
            # Should also send to DLQ
            raise Exception('Batch write to SQS failed - check DLQ')
    except Exception as e:
        print("{}".format(e))
        raise

    return response

def lambda_handler(event, context):
    for record in event['Records']:
        new_task = from_ddb_types(record['dynamodb']['NewImage'])
        old_task = from_ddb_types(record['dynamodb']['OldImage'])

        #
        # TASK DEPENDENCIES COMPLETED
        #

        if state_changed(new_task, old_task, 'task_depends_on') and \
            all([(v == DEPENDENCY_STATE_FINISHED) for v in new_task['task_depends_on'].values()]):
            state_table.update_task_status_to_pending(new_task['task_id'])

        #
        # TASK READY FOR EXECUTION
        #

        if state_changed(new_task, old_task, 'task_status', from_state=TASK_STATE_WAITING, target_state=TASK_STATE_PENDING):
            invocation_tstmp = get_time_now_ms()
            task_json_4_sqs: dict = copy.deepcopy(new_task)
            task_json_4_sqs["stats"] = {}
            task_json_4_sqs["stats"]["stage2_sbmtlmba_01_invocation_tstmp"]["tstmp"] = invocation_tstmp
            task_json_4_sqs["stats"]["stage2_sbmtlmba_02_before_batch_write_tstmp"]["tstmp"] = get_time_now_ms()
            write_to_sqs([{
                'Id': new_task['task_id'],  # use to return send result for this message
                'MessageBody': json.dumps(task_json_4_sqs)
            }], session_priority=new_task['task_priority'])
    
            

        #
        # TASK COMPLETED
        #

        if state_changed(new_task, old_task, 'task_status', target_state=TASK_STATE_FINISHED):
            # Update dependents
            if 'task_has_dependents' in new_task:
                for d in new_task['task_has_dependents']:
                    state_table.update_task_dependency_to_finished(new_task['task_id'], d)
