# Kubernetes Resource Recorder for AWS Config

> 📝 **Blog Post:** For a detailed walkthrough and use cases, see the [AWS blog post: Extending AWS Config to Kubernetes resources in Amazon EKS](https://aws.amazon.com/blogs/mt/extending-aws-config-to-kubernetes-resources-in-amazon-eks/)

This project extends AWS Config to record Kubernetes resources (such as Pods, Services, Deployments) from Amazon EKS clusters as custom resources in AWS Config. It enables you to track, audit, and evaluate the configuration of your Kubernetes resources alongside your AWS resources.

## Overview

AWS Config is a service that enables you to assess, audit, and evaluate the configurations of your AWS resources. With this project, you can extend AWS Config's capabilities to include Kubernetes resources from your Amazon EKS clusters, allowing you to manage both AWS and Kubernetes resources in a single place.

The solution periodically scans your EKS clusters, discovers Kubernetes resources, and records them as [AWS Config Custom Resources](https://docs.aws.amazon.com/config/latest/developerguide/customresources.html). This allows you to:

- Track configuration changes to Kubernetes resources over time
- Create AWS Config rules to evaluate Kubernetes resource configurations
- Include Kubernetes resources in your compliance reporting
- Query Kubernetes resources alongside AWS resources using AWS Config APIs
- Maintain a single source of truth for both AWS and Kubernetes configurations

## Deployment Instructions

### Prerequisites

- AWS CLI installed and configured
- [CloudFormation CLI (cfn)](https://docs.aws.amazon.com/cloudformation-cli/latest/userguide/what-is-cloudformation-cli.html) installed
- Python 3.13 or later
- An AWS account with permissions to create the required resources
- [AWS Config recorder](https://docs.aws.amazon.com/config/latest/developerguide/gs-console.html) must be enabled in the region where you deploy the solution
-  [API authentication mode](https://docs.aws.amazon.com/eks/latest/userguide/setting-up-access-entries.html) and  must be enabled on all the EKS clusters with resources that you want to record
- One of the following network configurations:
  - **Recommended**: A VPC and Subnets with connectivity to your EKS endpoints, AWS Config and S3 APIs (either via [private endpoints](https://docs.aws.amazon.com/vpc/latest/privatelink/privatelink-access-aws-services.html) or NAT Gateway for [Lambda internet access](https://docs.aws.amazon.com/lambda/latest/dg/configuration-vpc-internet.html)) - in this case, you will deploy the CloudFormation template with `VpcId` and `SubnetIds` parameters. For security reasons, AWS recommends providing private subnets and private endpoints.
  - EKS clusters configured with [public endpoint access](https://docs.aws.amazon.com/eks/latest/userguide/cluster-endpoint.html#modify-endpoint-access) - in this case, you could deploy the CloudFormation template without `VpcId` and `SubnetIds` parameters


### Deployment Steps

#### 1. Deploy the Custom Resource Type

First, you need to deploy the custom resource type that will represent Kubernetes resources in AWS Config:

```bash
# Navigate to the custom resource package directory
cd custom-resource-package

# Validate the schema
cfn validate

# Generate the resource provider package
cfn generate

# Submit the resource type to AWS CloudFormation
# Replace us-east-1 with your AWS region
cfn submit --region us-east-1 --set-default
```

#### 2. Build and Upload Lambda Functions

Run the build script to package Lambda functions and upload them to S3:

```bash
# Navigate to the project root
cd ..

# Run the build script
# Replace us-east-1 with your AWS region
./build.sh --region us-east-1
```

The build script will:
- Package the Lambda functions
- Upload them to S3 with a unique build ID in the path
- Generate a CloudFormation template with the build ID for deployment

#### 3. Deploy the CloudFormation Stack

After running the build script, you can deploy the generated CloudFormation template using the AWS CLI:

```bash
# Replace 123456789012 with your AWS account ID and us-east-1 with your region
aws cloudformation deploy \
    --template-file cloudformation.yaml \
    --stack-name KubernetesConfigManagement \
    --capabilities CAPABILITY_IAM \
    --parameter-overrides DeploymentBucketName=config-for-kubernetes-123456789012-us-east-1-deploy \
    --region us-east-1 \
    --no-fail-on-empty-changeset
```

### CloudFormation Parameters

| Parameter | Description | Default Value | Mandatory | Example |
|-----------|-------------|---------------|-----------|---------| 
| DeploymentBucketName | Name of the S3 bucket containing Lambda function code. This bucket must exist and contain the Lambda function packages uploaded by the build script. | *Empty* | Yes | config-for-kubernetes-123456789012-us-east-1-deploy (replace 123456789012 with your account ID and us-east-1 with your region) |
| ScheduleFrequency | Schedule expression for the state machine execution. Controls how frequently Kubernetes resources are scanned and recorded in AWS Config. Uses EventBridge schedule expressions. | rate(1 hour) | No | rate(30 minutes), cron(0 12 * * ? *) |
| ResourceKinds | Comma-separated list of Kubernetes resource kinds to scan. Specifies which Kubernetes resource types will be discovered and recorded in AWS Config. | Pod, Deployment, Service, ConfigMap, PersistentVolumeClaim, Ingress, StatefulSet, DaemonSet, Job, CronJob, NetworkPolicy, Namespace | No | Pod, Service, Deployment |
| LabelSelector | Kubernetes label selector to filter resources. Uses the same format as kubectl's label selectors to limit which resources are recorded based on their labels. | *Empty (no filtering)* | No | app=nginx, tier=frontend |
| IncludeNamespaces | Comma-separated list of namespaces to include. Only resources in these namespaces will be recorded. | *Empty (include all)* | No | default, kube-system |
| ExcludeNamespaces | Comma-separated list of namespaces to exclude. Resources in these namespaces will be ignored. | *Empty (exclude none)* | No | kube-system, kube-public |
| IncludeResourceFields | Comma-separated list of resource fields to include. Only these fields will be recorded for each resource. | *Empty (include all except excluded)* | No | metadata.name, metadata.namespace, spec |
| ExcludeResourceFields | Comma-separated list of resource fields to exclude. These fields will be omitted from recorded resources. | metadata.managedFields, status, data | No | metadata.annotations, spec.template |
| IncludeClusterTag | Tag key to include EKS clusters. Only clusters with this tag will be processed. | *Empty (include all)* | No | environment |
| ExcludeClusterTag | Tag key to exclude EKS clusters. Clusters with this tag will be skipped. | exclude-from-config-for-k8s | No | exclude-from-monitoring |
| VpcId | VPC ID where Lambda functions will run. Required only when accessing private EKS endpoints. The VPC must have connectivity to EKS endpoints and AWS services. | *Empty* | No | vpc-1234567890abcdef0 |
| SubnetIds | Comma-separated list of subnet IDs within the specified VPC. Required only when using VPC. Subnets must have proper routing to access required services. For High availability, provide multiple subnets in multiple Availability Zones | *Empty* | No | subnet-123456789, subnet-abcdef123 |

**Notes on parameters:**
- **ExcludeResourceFields**: The default value excludes:
  - `metadata.managedFields`: Server-added fields that change frequently and contain no relevant configuration information
  - `status`: Describes the actual state rather than desired configuration
  - `data`: Contains potentially sensitive information and is not relevant for configuration monitoring
- You cannot specify both `IncludeNamespaces` and `ExcludeNamespaces` at the same time.
- **IncludeClusterTag** and **ExcludeClusterTag** can be used together. If both are specified, a cluster must both have the include tag AND not have the exclude tag to be processed.

### Example Parameter Combinations

#### Example 1: Monitor only Deployments and Services in production namespaces

```bash
# Replace 123456789012 with your AWS account ID and us-east-1 with your region
aws cloudformation deploy \
    --template-file cloudformation.yaml \
    --stack-name KubernetesConfigManagement \
    --capabilities CAPABILITY_IAM \
    --parameter-overrides \
        DeploymentBucketName=config-for-kubernetes-123456789012-us-east-1-deploy \
        ResourceKinds=Deployment,Service \
        IncludeNamespaces=production,staging
```

#### Example 2: Monitor all resources with specific labels

```bash
# Replace 123456789012 with your AWS account ID and us-east-1 with your region
aws cloudformation deploy \
    --template-file cloudformation.yaml \
    --stack-name KubernetesConfigManagement \
    --capabilities CAPABILITY_IAM \
    --parameter-overrides \
        DeploymentBucketName=config-for-kubernetes-123456789012-us-east-1-deploy \
        LabelSelector=environment=production \
        ExcludeNamespaces=kube-system,kube-public
```

#### Example 3: Monitor only clusters with specific tags

```bash
# Replace 123456789012 with your AWS account ID and us-east-1 with your region
aws cloudformation deploy \
    --template-file cloudformation.yaml \
    --stack-name KubernetesConfigManagement \
    --capabilities CAPABILITY_IAM \
    --parameter-overrides \
        DeploymentBucketName=config-for-kubernetes-123456789012-us-east-1-deploy \
        IncludeClusterTag=monitor-config \
        ExcludeClusterTag=exclude-from-monitoring
```

## Testing Functionality After Deployment

After deploying the solution, you can verify that it's working correctly using the following methods:

### 1. Verify Step Functions Execution

1. Go to the [AWS Step Functions console](https://console.aws.amazon.com/states/home)
2. Open the state machine named "KubernetesConfigManagement"
3. Review past executions to see if they completed successfully
4. Inspect the execution results for details on discovered resources
5. To manually trigger an execution, choose **Start execution**

### 2. View Recorded Kubernetes Resources

1. Go to the [AWS Config console](https://console.aws.amazon.com/config/home)
2. On the left menu, choose **Resources**
3. For **Resource type**, select **AWSCustom EKS KubernetesResource** and choose **Apply**
4. You'll see all the Kubernetes resources that have been recorded
5. Select any resource to view its configuration details and history

### 3. Query Resources Using Advanced Query

1. Go to the [AWS Config console](https://console.aws.amazon.com/config/home)
2. On the left menu, choose **Advanced queries**
3. Choose **New query**
4. Enter the following query:
   ```
   SELECT
     *
   WHERE
     resourceType = 'AWSCustom::EKS::KubernetesResource'
   ```
5. Choose **Run** and inspect the results

You can extend this query to filter for specific Kubernetes resources, namespaces, or other attributes.

I'll add this cost section just before the Deployment Instructions section. Here's how it should look:

## Cost

You are responsible for the cost of the AWS services used while running this solution. A sample cost estimation for running this solution in the US East (N. Virginia) Region with 1,000 configuration changes per month across your Kubernetes resource is approximately $3.35 (USD) each month.

We recommend creating a [budget](https://docs.aws.amazon.com/cost-management/latest/userguide/budgets-create.html) through [AWS Cost Explorer](https://aws.amazon.com/aws-cost-management/aws-cost-explorer/) to help manage costs. Prices are subject to change. For full details, refer to the pricing webpage for each AWS service used in this solution.

### Sample cost table

The following table provides a sample cost breakdown for deploying this solution in the US East (N. Virginia) Region with 1,000 configuration changes per month across your Kubernetes resource, for one month.

| AWS service | Dimensions | Monthly cost [USD] |
|------------|------------|-------------------|
| AWS Config | 1,000 continuous configuration items | $3.00 |
| AWS Step Functions | 10,000 state transitions | $0.25 |
| AWS Lambda | 2,000 requests, 2GB memory, 3s average request duration | $0.20 |
| **Total** | | **$3.35** |

**Note**: Data transfer, Amazon Simple Storage Service (Amazon S3), Amazon EventBridge are priced less than $0.01 each month.

## Access Management Note

The solution automatically creates [EKS access entries](https://docs.aws.amazon.com/eks/latest/userguide/access-entries.html) for the Lambda execution role on each cluster that is processed. It associates the [`AmazonEKSViewPolicy`](https://docs.aws.amazon.com/eks/latest/userguide/access-policy-permissions.html#access-policy-permissions-amazoneksviewpolicy) (read-only) policy at the cluster level to enable resource discovery. This means the Lambda role will have read access to all resources in the clusters being monitored.
You can use the `IncludeClusterTag` and `ExcludeClusterTag` CloudFormation parameters to control which clusters are monitored and therefore which clusters receive access entries.

## License

Apache License 2.0 - see the LICENSE file for details.

## Limitations

- The solution only works with EKS clusters that have [API authentication mode](https://docs.aws.amazon.com/eks/latest/userguide/setting-up-access-entries.html) enabled
- There are limits to the resources that can be recorded in AWS Config:
  - Maximum size of each configuration item is 64KB as per the [PutResourceConfig API](https://docs.aws.amazon.com/config/latest/APIReference/API_PutResourceConfig.html).
  - Maximum number of configuration items supported is 100,000. Meaning that you can record up to 100,000 resources per account per region (regardless of how many EKS clusters you have deployed.).
