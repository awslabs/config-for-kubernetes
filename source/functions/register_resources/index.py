import boto3
import json
import logging
import hashlib
import os

logger = logging.getLogger()
logger.setLevel(logging.INFO)

CUSTOM_RESOURCE_TYPE = 'AWSCustom::EKS::KubernetesResource'

# Initialize AWS clients at module level for reuse across invocations
s3_client = boto3.client('s3')
config_client = boto3.client('config')
cfn_client = boto3.client('cloudformation')

def compute_resource_hash(resource_info):
    """Compute a hash of the resource info"""
    resource_info_json = json.dumps(resource_info, sort_keys=True)
    return resource_info_json, hashlib.md5(resource_info_json.encode(), usedforsecurity=False).hexdigest()

def save_current_hashes(bucket, current_hashes):
    """Save hashes for future runs, merging with previous hashes if they exist"""
    try:
        # Load previous hashes
        try:
            response = s3_client.get_object(
                Bucket=bucket,
                Key=f"hashes/resource_hashes.json"
            )
            previous_hashes = json.loads(response['Body'].read())
        except s3_client.exceptions.NoSuchKey:
            previous_hashes = {}
        except Exception as e:
            logger.error(f"Error loading previous hashes: {str(e)}")
            previous_hashes = {}

        # Merge previous and current hashes, with current taking precedence
        merged_hashes = {**previous_hashes, **current_hashes}

        # Save merged hashes
        s3_client.put_object(
            Bucket=bucket,
            Key=f"hashes/resource_hashes.json",
            Body=json.dumps(merged_hashes),
            ContentType='application/json'
        )
        logger.info(f"Saved merged hashes (previous: {len(previous_hashes)}, current: {len(current_hashes)}, total: {len(merged_hashes)})")
    except Exception as e:
        logger.error(f"Error saving hashes: {str(e)}")

def get_resource_type_version():
    """Get current version of the resource type"""
    response = cfn_client.describe_type(
        Type='RESOURCE',
        TypeName=CUSTOM_RESOURCE_TYPE
    )
    return response['DefaultVersionId']

def update_config_item(resource_id, resource_info, schema_version):
    """Update or create a resource in AWS Config"""
    try:
        config_client.put_resource_config(
            ResourceType=CUSTOM_RESOURCE_TYPE,
            ResourceId=resource_id,
            SchemaVersionId=schema_version,
            Configuration=resource_info
        )
        return True
    except Exception as e:
        logger.error(f"Error updating Config for resource {resource_id}: {str(e)}")
        return False

def lambda_handler(event, context):
    """
    Process a batch of resources and update AWS Config.
    """
    s3_bucket = event['s3Bucket']
    execution_id = event['executionId']
    batch_index = event['batchIndex']
    
    # Get schema version
    schema_version = get_resource_type_version()
    
    # Load data from S3
    try:
        response = s3_client.get_object(
            Bucket=s3_bucket,
            Key=f"executions/{execution_id}/to_update_{batch_index}.json"
        )
        resources = json.loads(response['Body'].read())
    except Exception as e:
        logger.error(f"Error loading resources for batch {batch_index}: {str(e)}")
        return {
            'status': 'error',
            'error': str(e)
        }
    
    current_hashes = {}
    
    updated_resources = 0
        
    # Process resources
    for resource in resources:
        resource_id = resource['resourceId']
        resource_info = resource['resourceInfo']
        
        # Convert to JSON for hashing and Config
        resource_info_json, current_hash = compute_resource_hash(resource_info)
        
        # Update Config
        success = update_config_item(
            resource_id, 
            resource_info_json, 
            schema_version
        )
        
        if success:
            updated_resources += 1
            current_hashes[resource_id] = current_hash
    
    # Save current hashes
    save_current_hashes(s3_bucket, current_hashes)
    
    return {
        'status': 'success',
        'batchIndex': batch_index,
        'totalToUpdate': len(resources),
        'totalUpdated': updated_resources
    }
