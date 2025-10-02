import boto3
import json
import logging
import os

logger = logging.getLogger()
logger.setLevel(logging.INFO)

CUSTOM_RESOURCE_TYPE = 'AWSCustom::EKS::KubernetesResource'

# Initialize AWS clients at module level for reuse across invocations
s3_client = boto3.client('s3')
config_client = boto3.client('config')

def lambda_handler(event, context):
    s3_bucket = event['s3Bucket']
    execution_id = event['executionId']
    batch_index = event['batchIndex']

    # Load resources to delete from S3
    try:
        response = s3_client.get_object(
            Bucket=s3_bucket,
            Key=f"executions/{execution_id}/to_delete_{batch_index}.json"
        )
        to_delete_list = json.loads(response['Body'].read())
    except Exception as e:
        logger.error(f"Error loading resources to delete: {str(e)}")
        return {
            'status': 'error',
            'error': f"Failed to load resources to delete: {str(e)}"
        }

    # Delete resources
    delete_count = 0
    for resource_id in to_delete_list:
        try:
            config_client.delete_resource_config(
                ResourceType=CUSTOM_RESOURCE_TYPE,
                ResourceId=resource_id
            )
            delete_count += 1
        except Exception as e:
            logger.error(f"Error deleting resource {resource_id}: {str(e)}")
    
    # Update hashes file by removing deleted resources
    try:
        # Load current hashes
        try:
            response = s3_client.get_object(
                Bucket=s3_bucket,
                Key=f"hashes/resource_hashes.json"
            )
            current_hashes = json.loads(response['Body'].read())
        except s3_client.exceptions.NoSuchKey:
            current_hashes = {}
        except Exception as e:
            logger.error(f"Error loading hashes file: {str(e)}")
            current_hashes = {}

        # Remove deleted resources from hashes
        initial_hash_count = len(current_hashes)
        for resource_id in to_delete_list:
            current_hashes.pop(resource_id, None)
        
        # Save updated hashes back to S3
        s3_client.put_object(
            Bucket=s3_bucket,
            Key=f"hashes/resource_hashes.json",
            Body=json.dumps(current_hashes),
            ContentType='application/json'
        )
        logger.info(f"Updated hashes file: removed {initial_hash_count - len(current_hashes)} entries")
    except Exception as e:
        logger.error(f"Error updating hashes file: {str(e)}")

    return {
        'status': 'success',
        'batchIndex': batch_index,
        'totalToDelete': len(to_delete_list),
        'totalDeleted': delete_count
    }
