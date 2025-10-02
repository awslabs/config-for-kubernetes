#!/bin/bash
set -e

# Parse command line arguments
usage() {
    echo "Usage: $0 [--region REGION]"
    echo "  --region REGION    AWS region to deploy to (default: uses AWS CLI configured region)"
    exit 1
}

# Default to the configured region
REGION=$(aws configure get region)

# Parse arguments
while [[ "$#" -gt 0 ]]; do
    case $1 in
        --region) REGION="$2"; shift 2 ;;
        --help) usage ;;
        *) echo "Unknown parameter: $1"; usage ;;
    esac
done

if [ -z "$REGION" ]; then
    echo "Error: No region specified and no default region configured in AWS CLI"
    usage
fi

echo "Using AWS region: $REGION"

# Create a temporary directory for building
BUILD_DIR=$(mktemp -d)
echo "Using temporary directory: $BUILD_DIR"

# Get the script directory
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
FUNCTIONS_DIR="$SCRIPT_DIR/source/functions"

# Create deployment S3 bucket if it doesn't exist
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text --region $REGION)
DEPLOY_BUCKET_NAME="config-for-kubernetes-${ACCOUNT_ID}-${REGION}-deploy"

echo "Checking if deployment bucket $DEPLOY_BUCKET_NAME exists..."
if ! aws s3api head-bucket --bucket "$DEPLOY_BUCKET_NAME" 2>/dev/null; then
    echo "Creating deployment S3 bucket: $DEPLOY_BUCKET_NAME"
    aws s3 mb "s3://$DEPLOY_BUCKET_NAME" --region "$REGION"
    aws s3api put-bucket-versioning --bucket "$DEPLOY_BUCKET_NAME" --versioning-configuration Status=Enabled
    aws s3api put-bucket-encryption --bucket "$DEPLOY_BUCKET_NAME" --server-side-encryption-configuration '{
        "Rules": [
            {
                "ApplyServerSideEncryptionByDefault": {
                    "SSEAlgorithm": "AES256"
                }
            }
        ]
    }'
else
    echo "Deployment bucket $DEPLOY_BUCKET_NAME already exists"
fi

# Generate a unique BUILD_ID for this deployment
BUILD_ID=$(date +%Y%m%d%H%M%S)-$(< /dev/urandom LC_ALL=C tr -dc 'a-z0-9' | fold -w 8 | head -n 1)
echo "Using BUILD_ID: $BUILD_ID"

# Function to package and upload a Lambda function
package_function() {
    local function_name=$1
    local function_dir="$FUNCTIONS_DIR/$function_name"
    local zip_file="$BUILD_DIR/${function_name}.zip"
    local s3_key="functions/${BUILD_ID}/${function_name}.zip"
    
    echo "Packaging $function_name..."
    
    # Check if requirements.txt exists
    if [ -f "$function_dir/requirements.txt" ]; then
        echo "Installing dependencies for $function_name..."
        python3 -m pip install -r "$function_dir/requirements.txt" -t "$BUILD_DIR/package" --no-cache-dir
        
        # Copy function code to package directory
        cp "$function_dir"/*.py "$BUILD_DIR/package/"
        
        # Create zip file
        cd "$BUILD_DIR/package"
        zip -r "$zip_file" .
        cd - > /dev/null
    else
        # Just zip the function code
        cd "$function_dir"
        zip -r "$zip_file" .
        cd - > /dev/null
    fi
    
    # Upload to S3
    echo "Uploading $function_name to S3..."
    aws s3 cp "$zip_file" "s3://$DEPLOY_BUCKET_NAME/$s3_key" --region "$REGION"
    
    echo "Successfully packaged and uploaded $function_name"
}

# Package and upload each function
package_function "discover_resources"
package_function "register_resources"
package_function "delete_resources"

# Create the CloudFormation template with the BUILD_ID
echo "Creating CloudFormation deployment template..."
sed "s/{{BUILD_ID}}/$BUILD_ID/g" "$SCRIPT_DIR/source/cloudformation-source-DO-NOT-DEPLOY.yaml" > "$SCRIPT_DIR/cloudformation.yaml"

# Clean up
rm -rf "$BUILD_DIR"
echo "Build completed successfully!"

echo ""
echo "==================================================================="
echo "Lambda functions have been packaged and uploaded to S3."
echo ""
echo "To deploy the CloudFormation stack, run:"
echo "aws cloudformation deploy \\"
echo "    --template-file \"$SCRIPT_DIR/cloudformation.yaml\" \\"
echo "    --stack-name KubernetesConfigManagement \\"
echo "    --capabilities CAPABILITY_IAM \\"
echo "    --parameter-overrides DeploymentBucketName=$DEPLOY_BUCKET_NAME \\"
echo "    --region $REGION \\"
echo "    --no-fail-on-empty-changeset"
echo "==================================================================="
