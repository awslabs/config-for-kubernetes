import boto3
import logging
import json
import os
import sys
from kubernetes import client, config
import base64
import re
from botocore.signers import RequestSigner
from kubernetes.dynamic import DynamicClient
import time
import hashlib

logger = logging.getLogger()
logger.setLevel(logging.INFO)

S3_BUCKET = os.environ['S3_BUCKET']
LAMBDA_ROLE = os.environ['LAMBDA_ROLE']
CUSTOM_RESOURCE_TYPE = 'AWSCustom::EKS::KubernetesResource'
STS_TOKEN_EXPIRES_IN = 60
BATCH_SIZE = 2000

# Get filtering parameters from environment variables
resource_kinds_str = os.environ['RESOURCE_KINDS']
label_selector = os.environ['LABEL_SELECTOR']

include_namespaces_str = os.environ['INCLUDE_NAMESPACES']
exclude_namespaces_str = os.environ['EXCLUDE_NAMESPACES']

include_resource_fields_str = os.environ['INCLUDE_RESOURCE_FIELDS']
exclude_resource_fields_str = os.environ['EXCLUDE_RESOURCE_FIELDS']

# Get cluster tag filtering parameters (optional)
include_cluster_tag = os.environ.get('INCLUDE_CLUSTER_TAG', '')
exclude_cluster_tag = os.environ.get('EXCLUDE_CLUSTER_TAG', '')

# Parse resource kinds
resource_kinds = list(set(filter(None, [kind.strip() for kind in resource_kinds_str.split(',')])))

# Parse namespace filters
include_namespaces = set(filter(None, [ns.strip() for ns in include_namespaces_str.split(',')]))
exclude_namespaces = set(filter(None, [ns.strip() for ns in exclude_namespaces_str.split(',')]))

# Parse resource fields filters
include_resource_fields = list(set(filter(None, [field.strip() for field in include_resource_fields_str.split(',')])))
exclude_resource_fields = list(set(filter(None, [field.strip() for field in exclude_resource_fields_str.split(',')])))

# Validate parameters
if not resource_kinds:
    logger.error("No resource kinds specified. Exiting.")
    raise ValueError("No resource kinds specified.")
    
if include_namespaces and exclude_namespaces:
    logger.error("Both INCLUDE_NAMESPACES and EXCLUDE_NAMESPACES are set. Please use only one. Exiting.")
    sys.exit("Both INCLUDE_NAMESPACES and EXCLUDE_NAMESPACES are set. Please use only one.")

# Initialize AWS clients at module level for reuse across invocations
session = boto3.session.Session()
sts = session.client('sts')
s3_client = boto3.client('s3')
eks_client = boto3.client('eks')
config_client = boto3.client('config')
service_id = sts.meta.service_model.service_id

def compute_resource_hash(resource_info):
    """Compute a hash of the resource info"""
    resource_info_json = json.dumps(resource_info, sort_keys=True)
    return resource_info_json, hashlib.md5(resource_info_json.encode(), usedforsecurity=False).hexdigest()

def get_bearer_token(cluster_name):
    """Create authentication token for EKS"""
    signer = RequestSigner(
        service_id,
        session.region_name,
        'sts',
        'v4',
        session.get_credentials(),
        session.events
    )

    params = {
        'method': 'GET',
        'url': f'https://sts.{session.region_name}.amazonaws.com/?Action=GetCallerIdentity&Version=2011-06-15',
        'body': {},
        'headers': {'x-k8s-aws-id': cluster_name},
        'context': {}
    }

    signed_url = signer.generate_presigned_url(
        params,
        region_name=session.region_name,
        expires_in=STS_TOKEN_EXPIRES_IN,
        operation_name=''
    )
    base64_url = base64.urlsafe_b64encode(signed_url.encode('utf-8')).decode('utf-8')
    return 'k8s-aws-v1.' + re.sub(r'=*', '', base64_url)

def ensure_eks_access(cluster_name):
    """Ensure the Lambda has access to the EKS cluster"""
    view_policy_arn = 'arn:aws:eks::aws:cluster-access-policy/AmazonEKSViewPolicy'
    
    try:
        # Check if role already has access
        eks_client.describe_access_entry(
            clusterName=cluster_name,
            principalArn=LAMBDA_ROLE
        )
        logger.info(f"Role {LAMBDA_ROLE} already has access to cluster {cluster_name}")
    except eks_client.exceptions.ResourceNotFoundException:
        # Create access entry if it doesn't exist
        logger.info(f"Creating access entry for {LAMBDA_ROLE} in cluster {cluster_name}")
        eks_client.create_access_entry(
            clusterName=cluster_name,
            principalArn=LAMBDA_ROLE
        )
        eks_client.associate_access_policy(
            clusterName=cluster_name,
            principalArn=LAMBDA_ROLE,
            policyArn=view_policy_arn,
            accessScope={'type': 'cluster'}
        )
        logger.info(f"Successfully created access entry for {LAMBDA_ROLE} in cluster {cluster_name}")
        time.sleep(2)  # Wait for the access entry to be propagated

def should_process_namespace(namespace, include_namespaces, exclude_namespaces):
    """Determine if a namespace should be processed based on filters"""
    if include_namespaces:
        return namespace in include_namespaces
    elif exclude_namespaces:
        return namespace not in exclude_namespaces
    return True

def filter_resource_fields(item_dict, include_resource_fields, exclude_resource_fields):
    """Filter fields from a resource based on include/exclude filters"""
    result = item_dict.copy()
    
    if include_resource_fields:
        # If include fields are specified, start with an empty dict and only add included fields
        filtered = {}
        for field in include_resource_fields:
            parts = field.split('.')
            value = item_dict
            for part in parts:
                if isinstance(value, dict) and part in value:
                    value = value[part]
                else:
                    value = None
                    break
            if value is not None:
                current = filtered
                for part in parts[:-1]:
                    if part not in current:
                        current[part] = {}
                    current = current[part]
                current[parts[-1]] = value
        return filtered
    
    elif exclude_resource_fields:
        # If exclude fields are specified, remove them from the resource
        def recursive_remove(obj, field_path):
            if '.' in field_path:
                current, rest = field_path.split('.', 1)
                if isinstance(obj, dict) and current in obj:
                    recursive_remove(obj[current], rest)
                    if not obj[current]:  # Remove empty dict
                        del obj[current]
            else:
                if isinstance(obj, dict) and field_path in obj:
                    del obj[field_path]
        
        for field in exclude_resource_fields:
            recursive_remove(result, field)
    
    return result

def generate_resource_id(cluster, kind, name, namespace=None):
    """Generate a unique ID for a Kubernetes resource"""
    if namespace is None:
        return f"{cluster}/{kind}/{name}"
    else:
        return f"{cluster}/{namespace}/{kind}/{name}"

def load_previous_hashes(bucket):
    """Load hashes from previous runs"""
    try:
        response = s3_client.get_object(
            Bucket=bucket,
            Key=f"hashes/resource_hashes.json"
        )
        return json.loads(response['Body'].read())
    except s3_client.exceptions.NoSuchKey:
        return {}
    except Exception as e:
        logger.error(f"Error loading previous hashes: {str(e)}")
        return {}

def flush_resources_batch(all_resources, to_register_list, execution_id):
    if len(all_resources) > 0:
        # Add a new batch
        current_batch = len(to_register_list)
        s3_client.put_object(
            Bucket=S3_BUCKET,
            Key=f"executions/{execution_id}/to_update_{current_batch}.json",
            Body=json.dumps(all_resources),
            ContentType='application/json'
        )
        all_resources.clear()
        to_register_list.append(current_batch)

def configure_kubernetes_client(cluster_name):
    """Configure and return a Kubernetes dynamic client for the specified cluster"""
    # Get cluster info
    cluster_info = eks_client.describe_cluster(name=cluster_name)
    cluster_endpoint = cluster_info['cluster']['endpoint']
    cluster_ca = cluster_info['cluster']['certificateAuthority']['data']
    
    # Configure kubernetes client
    kubeconfig = {
        'apiVersion': 'v1',
        'clusters': [{
        'name': 'cluster1',
        'cluster': {
            'certificate-authority-data': cluster_ca,
            'server': cluster_endpoint}
        }],
        'contexts': [{'name': 'context1', 'context': {'cluster': 'cluster1', "user": "user1"}}],
        'current-context': 'context1',
        'kind': 'Config',
        'preferences': {},
        'users': [{'name': 'user1', "user": {'token': get_bearer_token(cluster_name)}}]
    }

    config.load_kube_config_from_dict(config_dict=kubeconfig)
    
    # Create and return dynamic client
    return DynamicClient(client.ApiClient())

def process_resource_item(
    item, 
    cluster_name, 
    kind, 
    is_kind_namespaced,
    include_namespaces, 
    exclude_namespaces, 
    include_resource_fields, 
    exclude_resource_fields,
    all_resources,
    current_resource_ids,
    to_register_list,
    previous_hashes,
    execution_id
):
    """Process a single Kubernetes resource item"""
    item_dict = item.to_dict()
    name = item_dict['metadata']['name']
    namespace = item_dict['metadata'].get('namespace')
    
    # Apply namespace filtering only for namespaced resources
    if is_kind_namespaced and not should_process_namespace(namespace, include_namespaces, exclude_namespaces):
        return
    
    # Apply field filtering
    item_dict = filter_resource_fields(item_dict, include_resource_fields, exclude_resource_fields)
    
    resource_id = generate_resource_id(cluster_name, kind, name, namespace)
    
    resource_info = {
        "resourceId": resource_id,
        "resourceInfo": {
            "EksClusterName": cluster_name,
            "K8sResourceKind": kind,
            "K8sResourceName": name,
            "K8sItem": item_dict
        }
    }
    if namespace:
        resource_info["resourceInfo"]["K8sNamespace"] = namespace

    _, current_hash = compute_resource_hash(resource_info['resourceInfo'])

    current_resource_ids.add(resource_id)

    # Check if resource has changed
    if not (resource_id in previous_hashes and previous_hashes[resource_id] == current_hash):
        all_resources.append(resource_info)

    if len(all_resources) == BATCH_SIZE:
        flush_resources_batch(all_resources, to_register_list, execution_id)

def fetch_resources_with_pagination(
    resource, 
    kind, 
    label_selector, 
    cluster_name,
    include_namespaces, 
    exclude_namespaces, 
    include_resource_fields, 
    exclude_resource_fields,
    all_resources,
    current_resource_ids,
    to_register_list,
    previous_hashes,
    execution_id
):
    """Fetch resources using pagination and process each batch"""
    is_kind_namespaced = resource.namespaced
    
    # Use pagination to get resources in batches
    continue_token = None
    page_count = 0
    total_items = 0
    
    while True:
        page_count += 1
        
        # Prepare request parameters
        params = {'limit': BATCH_SIZE}
        if label_selector:
            params['label_selector'] = label_selector
        if continue_token:
            params['_continue'] = continue_token
        
        # Get batch of resources
        response = resource.get(**params)
        items = response.items
        batch_size = len(items)
        total_items += batch_size

        continue_token = response.metadata.get("continue")
        
        # Log progress
        remaining = 0
        if continue_token:
            remaining = response.metadata.get("remainingItemCount", 0)
            # Sleep to add interval between each execution of calls to Kubernetes API server
            time.sleep(1)
        
        logger.info(f"Processing {kind} page {page_count}: {batch_size} items (remaining: {remaining})")
        
        # Process each item in the batch
        for item in items:
            process_resource_item(
                item, 
                cluster_name, 
                kind, 
                is_kind_namespaced,
                include_namespaces, 
                exclude_namespaces, 
                include_resource_fields, 
                exclude_resource_fields,
                all_resources,
                current_resource_ids,
                to_register_list,
                previous_hashes,
                execution_id
            )
        
        # Check if there are more items
        if not continue_token:
            break
    
    logger.info(f"Completed processing {total_items} {kind}(s) in cluster {cluster_name}")

def list_resources_by_type(
    cluster_name,
    resource_kinds,
    label_selector,
    include_namespaces,
    exclude_namespaces,
    include_resource_fields,
    exclude_resource_fields,
    
    all_resources,
    current_resource_ids,
    to_register_list,
    previous_hashes,
    execution_id):

    """List resources in the cluster by resource kind with filtering"""
    
    # Configure kubernetes client
    dyn_client = configure_kubernetes_client(cluster_name)
    
    # Process each resource kind
    for kind in resource_kinds:
        try:
            resource = dyn_client.resources.get(kind=kind)
            
            # Fetch and process resources with pagination
            fetch_resources_with_pagination(
                resource, 
                kind, 
                label_selector, 
                cluster_name,
                include_namespaces, 
                exclude_namespaces, 
                include_resource_fields, 
                exclude_resource_fields,
                all_resources,
                current_resource_ids,
                to_register_list,
                previous_hashes,
                execution_id
            )
            
        except Exception as e:
            error_msg = f"Error listing {kind} resources in cluster {cluster_name}: {str(e)}"
            logger.error(error_msg)
            # Raise exception to fail the Lambda function and prevent the step function from continuing
            raise Exception(error_msg)

def get_config_resources_ids():
    """List all resources in AWS Config"""
    resources = []
    
    paginator = config_client.get_paginator('list_discovered_resources')
    for page in paginator.paginate(resourceType=CUSTOM_RESOURCE_TYPE):
        resources.extend([r['resourceId'] for r in page['resourceIdentifiers']])
    
    return set(resources)

def log_filtering_parameters():
    """Log the filtering parameters for the current execution"""
    logger.info(f"Resource kinds: {resource_kinds}")
    if label_selector:
        logger.info(f"Using label selector: {label_selector}")
    else:
        logger.info("No label selector specified")
        
    if include_namespaces:
        logger.info(f"Including only these namespaces: {include_namespaces}")
    elif exclude_namespaces:
        logger.info(f"Excluding these namespaces: {exclude_namespaces}")
    else:
        logger.info("Processing all namespaces")
        
    if include_resource_fields:
        logger.info(f"Including only these resource fields: {include_resource_fields}")
    elif exclude_resource_fields:
        logger.info(f"Excluding these resource fields: {exclude_resource_fields}")
    else:
        logger.info("Processing all resource fields")
        
    logger.info(f"S3 Bucket: {S3_BUCKET}")

def list_eks_clusters():
    """List all EKS clusters in the account"""
    clusters = []
    paginator = eks_client.get_paginator('list_clusters')
    for page in paginator.paginate():
        clusters.extend(page['clusters'])
    
    logger.info(f"Found {len(clusters)} EKS clusters")
    return clusters

def should_process_cluster(cluster_name):
    """Determine if a cluster should be processed based on its status, auth mode, and tags"""
    # Get cluster details
    cluster = eks_client.describe_cluster(name=cluster_name)
    
    # Check cluster status
    if cluster['cluster']['status'] != 'ACTIVE':
        logger.info(f"Skipping cluster {cluster_name}: status is not ACTIVE")
        return False

    # Check authentication mode
    if cluster['cluster']['accessConfig']['authenticationMode'] not in ['API', 'API_AND_CONFIG_MAP']:
        logger.info(f"Skipping cluster {cluster_name}: authentication mode is not API nor API_AND_CONFIG_MAP")
        return False
        
    # Check for include/exclude tags
    cluster_tags = cluster['cluster'].get('tags', {})
    
    # Skip if exclude tag is present
    if exclude_cluster_tag and exclude_cluster_tag in cluster_tags:
        logger.info(f"Skipping cluster {cluster_name}: exclude tag '{exclude_cluster_tag}' is present")
        return False
        
    # Skip if include tag is specified but not present on the cluster
    if include_cluster_tag and include_cluster_tag not in cluster_tags:
        logger.info(f"Skipping cluster {cluster_name}: include tag '{include_cluster_tag}' is not present")
        return False
    
    return True

def process_cluster(
    cluster_name, 
    all_resources, 
    current_resource_ids, 
    to_register_list, 
    previous_hashes, 
    execution_id
):
    """Process a single cluster to discover resources"""
    # Ensure we have access to the cluster
    ensure_eks_access(cluster_name)
    
    # List resources in the cluster by kind with filtering
    list_resources_by_type(
        cluster_name, 
        resource_kinds, 
        label_selector, 
        include_namespaces, 
        exclude_namespaces, 
        include_resource_fields, 
        exclude_resource_fields,
        all_resources,
        current_resource_ids,
        to_register_list,
        previous_hashes,
        execution_id
    )

def store_resources_to_delete(to_delete, execution_id):
    """Store resources to delete in S3 and return the list of batch indices"""
    to_delete_count = 0
    to_delete_list = []
    
    for i in range(0, len(to_delete), BATCH_SIZE):
        batch = to_delete[i:i+BATCH_SIZE]
        s3_client.put_object(
            Bucket=S3_BUCKET,
            Key=f"executions/{execution_id}/to_delete_{to_delete_count}.json",
            Body=json.dumps(batch),
            ContentType='application/json'
        )
        to_delete_list.append(to_delete_count)
        to_delete_count += 1
    
    return to_delete_list

def lambda_handler(event, context):
    """
    Main function to discover resources.
    """
    
    execution_id = event['executionId']
    
    # Log filtering parameters
    log_filtering_parameters()
    
    # List all EKS clusters
    clusters = list_eks_clusters()
    
    # Get existing Config resources
    config_resources_ids = get_config_resources_ids()
    logger.info(f"Found {len(config_resources_ids)} resources in AWS Config")
    
    all_resources = []
    current_resource_ids = set()
    to_register_list = []
    previous_hashes = load_previous_hashes(S3_BUCKET)
    
    # Process each cluster
    for cluster_name in clusters:
        if should_process_cluster(cluster_name):
            process_cluster(
                cluster_name, 
                all_resources, 
                current_resource_ids, 
                to_register_list, 
                previous_hashes, 
                execution_id
            )
    
    flush_resources_batch(all_resources, to_register_list, execution_id)

    # Calculate resources to delete
    to_delete = list(config_resources_ids - current_resource_ids)
    
    # Store resources in S3
    to_delete_list = store_resources_to_delete(to_delete, execution_id)
    
    # Return result for next step
    return {
        'toRegisterList': to_register_list,
        'toDeleteList': to_delete_list,
        's3Bucket': S3_BUCKET,
        'executionId': execution_id
    }
