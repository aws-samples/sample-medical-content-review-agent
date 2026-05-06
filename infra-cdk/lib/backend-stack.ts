// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import * as cdk from "aws-cdk-lib"
import * as cognito from "aws-cdk-lib/aws-cognito"
import * as iam from "aws-cdk-lib/aws-iam"
import * as ssm from "aws-cdk-lib/aws-ssm"
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager"
import * as dynamodb from "aws-cdk-lib/aws-dynamodb"
import * as apigateway from "aws-cdk-lib/aws-apigateway"
import * as logs from "aws-cdk-lib/aws-logs"
import * as s3 from "aws-cdk-lib/aws-s3"
import * as agentcore from "@aws-cdk/aws-bedrock-agentcore-alpha"
import * as bedrockagentcore from "aws-cdk-lib/aws-bedrockagentcore"
import { PythonFunction } from "@aws-cdk/aws-lambda-python-alpha"
import * as lambda from "aws-cdk-lib/aws-lambda"
import * as ecr_assets from "aws-cdk-lib/aws-ecr-assets"
import * as cr from "aws-cdk-lib/custom-resources"
import * as cloudwatch from "aws-cdk-lib/aws-cloudwatch"
import * as cw_actions from "aws-cdk-lib/aws-cloudwatch-actions"
import * as sns from "aws-cdk-lib/aws-sns"
import * as wafv2 from "aws-cdk-lib/aws-wafv2"
import { Construct, IConstruct } from "constructs"
import { AppConfig } from "./utils/config-manager"
import { AgentCoreRole } from "./utils/agentcore-role"
import * as path from "path"
import * as fs from "fs"

export interface BackendStackProps extends cdk.NestedStackProps {
  config: AppConfig
  userPoolId: string
  userPoolClientId: string
  userPoolDomain: cognito.UserPoolDomain
  frontendUrl: string
  stagingBucket: s3.Bucket
}

export class BackendStack extends cdk.NestedStack {
  public readonly userPoolId: string
  public readonly userPoolClientId: string
  public readonly userPoolDomain: cognito.UserPoolDomain
  public feedbackApiUrl: string
  public runtimeArn: string
  public memoryArn: string
  private agentName: cdk.CfnParameter
  private networkMode: cdk.CfnParameter
  private userPool: cognito.IUserPool
  private machineClient: cognito.UserPoolClient
  private agentRuntime: agentcore.Runtime
  private stagingBucketName: string
  private stagingBucket: s3.Bucket
  private alarmTopic: sns.Topic

  constructor(scope: Construct, id: string, props: BackendStackProps) {
    super(scope, id, props)

    // Store the Cognito values
    this.userPoolId = props.userPoolId
    this.userPoolClientId = props.userPoolClientId
    this.userPoolDomain = props.userPoolDomain
    this.stagingBucketName = props.stagingBucket.bucketName
    this.stagingBucket = props.stagingBucket

    // Single SNS topic used by all Lambda CloudWatch alarms in this stack.
    // No subscription configured; add an email subscription when needed.
    this.alarmTopic = new sns.Topic(this, "LambdaAlarmTopic", {
      displayName: `${props.config.stack_name_base}-lambda-alarms`,
    })

    // Ensure X-Ray active tracing on every Lambda in this stack, including
    // CDK-generated singleton functions (e.g. s3 autoDeleteObjects handler,
    // cr.Provider framework Lambdas) that cannot be referenced directly.
    // Matches by synthesized CFN type so it catches both lambda.CfnFunction and
    // CustomResourceProvider-generated Lambdas.
    const stackSelf = this
    cdk.Aspects.of(this).add({
      visit(node: IConstruct) {
        if (
          cdk.CfnResource.isCfnResource(node) &&
          node.cfnResourceType === "AWS::Lambda::Function"
        ) {
          node.addPropertyOverride("TracingConfig", { Mode: "Active" })
          stackSelf.ensureCfnLambdaErrorsAlarm(node)
        }
      },
    })

    // Import the Cognito resources from the other stack
    this.userPool = cognito.UserPool.fromUserPoolId(
      this,
      "ImportedUserPoolForBackend",
      props.userPoolId
    )
    // then create the user pool client
    cognito.UserPoolClient.fromUserPoolClientId(
      this,
      "ImportedUserPoolClient",
      props.userPoolClientId
    )

    // Create Machine-to-Machine authentication components
    this.createMachineAuthentication(props.config)

    // DEPLOYMENT ORDER EXPLANATION:
    // 1. Cognito User Pool & Client (created in separate CognitoStack)
    // 2. Machine Client & Resource Server (created above for M2M auth)
    // 3. AgentCore Gateway (created next - uses machine client for auth)
    // 4. AgentCore Runtime (created last - independent of gateway)
    //
    // This order ensures that authentication components are available before
    // the gateway that depends on them, while keeping the runtime separate
    // since it doesn't directly depend on the gateway.

    // Create AgentCore Gateway (before Runtime)
    this.createAgentCoreGateway(props.config)

    // Create AgentCore Runtime resources
    this.createAgentCoreRuntime(props.config)

    // Store runtime ARN in SSM for frontend stack
    this.createRuntimeSSMParameters(props.config)

    // Store Cognito configuration in SSM for testing and frontend
    this.createCognitoSSMParameters(props.config)

    // Create Feedback DynamoDB table (example of application data storage)
    const feedbackTable = this.createFeedbackTable(props.config)

    // Create API Gateway Feedback API resources (example of best-practice API Gateway + Lambda
    // pattern)
    this.createFeedbackApi(props.config, props.frontendUrl, feedbackTable)
  }

  private createAgentCoreRuntime(config: AppConfig): void {
    const pattern = config.backend?.pattern || "medical-content-review"

    // Parameters
    this.agentName = new cdk.CfnParameter(this, "AgentName", {
      type: "String",
      default: "StrandsAgent",
      description: "Name for the agent runtime",
    })

    this.networkMode = new cdk.CfnParameter(this, "NetworkMode", {
      type: "String",
      default: "PUBLIC",
      description: "Network mode for AgentCore resources",
      allowedValues: ["PUBLIC", "PRIVATE"],
    })

    const stack = cdk.Stack.of(this)
    const deploymentType = config.backend.deployment_type

    // Create the agent runtime artifact based on deployment type
    let agentRuntimeArtifact: agentcore.AgentRuntimeArtifact
    let zipPackagerResource: cdk.CustomResource | undefined

    if (deploymentType === "zip") {
      // ZIP DEPLOYMENT: Use Lambda to package and upload to S3 (no Docker required)
      const repoRoot = path.resolve(__dirname, "..", "..")
      // nosemgrep: path-join-resolve-traversal -- build-time paths from validated config, not user input
      const patternDir = path.join(repoRoot, "patterns", pattern)

      // Create S3 bucket for agent code
      const agentCodeBucket = new s3.Bucket(this, "AgentCodeBucket", {
        removalPolicy: cdk.RemovalPolicy.DESTROY,
        autoDeleteObjects: true,
        versioned: true,
        blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      })

      // Lambda to package agent code
      const packagerLambda = new lambda.Function(this, "ZipPackagerLambda", {
        runtime: lambda.Runtime.PYTHON_3_12,
        handler: "index.handler",
        code: lambda.Code.fromAsset(path.join(__dirname, "..", "lambdas", "zip-packager")),
        timeout: cdk.Duration.minutes(10),
        memorySize: 1024,
        ephemeralStorageSize: cdk.Size.gibibytes(2),
        tracing: lambda.Tracing.ACTIVE,
      })

      agentCodeBucket.grantReadWrite(packagerLambda)

      // Read agent code files and encode as base64
      const agentCode: Record<string, string> = {}

      // Read pattern .py files
      for (const file of fs.readdirSync(patternDir)) {
        if (file.endsWith(".py")) {
          // nosemgrep: path-join-resolve-traversal
          const content = fs.readFileSync(path.join(patternDir, file))
          agentCode[file] = content.toString("base64")
        }
      }

      // Read shared modules (gateway/, tools/)
      for (const module of ["gateway", "tools"]) {
        const moduleDir = path.join(repoRoot, module)
        if (fs.existsSync(moduleDir)) {
          this.readDirRecursive(moduleDir, module, agentCode)
        }
      }

      // Read requirements
      // nosemgrep: path-join-resolve-traversal
      const requirementsPath = path.join(patternDir, "requirements.txt")
      const requirements = fs
        .readFileSync(requirementsPath, "utf-8")
        .split("\n")
        .map(line => line.trim())
        .filter(line => line && !line.startsWith("#"))

      // Create hash for change detection
      // We use this to trigger update when content changes
      const contentHash = this.hashContent(JSON.stringify({ requirements, agentCode }))

      // Custom Resource to trigger packaging
      const provider = new cr.Provider(this, "ZipPackagerProvider", {
        onEventHandler: packagerLambda,
      })

      zipPackagerResource = new cdk.CustomResource(this, "ZipPackager", {
        serviceToken: provider.serviceToken,
        properties: {
          BucketName: agentCodeBucket.bucketName,
          ObjectKey: "deployment_package.zip",
          Requirements: requirements,
          AgentCode: agentCode,
          ContentHash: contentHash,
        },
      })

      // Store bucket name in SSM for updates
      new ssm.StringParameter(this, "AgentCodeBucketNameParam", {
        parameterName: `/${config.stack_name_base}/agent-code-bucket`,
        stringValue: agentCodeBucket.bucketName,
        description: "S3 bucket for agent code deployment packages",
      })

      agentRuntimeArtifact = agentcore.AgentRuntimeArtifact.fromS3(
        {
          bucketName: agentCodeBucket.bucketName,
          objectKey: "deployment_package.zip",
        },
        agentcore.AgentCoreRuntime.PYTHON_3_12,
        ["opentelemetry-instrument", "basic_agent.py"]
      )
    } else {
      // DOCKER DEPLOYMENT: Use container-based deployment
      agentRuntimeArtifact = agentcore.AgentRuntimeArtifact.fromAsset(
        path.resolve(__dirname, "..", ".."),
        {
          platform: ecr_assets.Platform.LINUX_ARM64,
          file: `patterns/${pattern}/Dockerfile`,
        }
      )
    }

    // Configure network mode
    const networkConfiguration =
      this.networkMode.valueAsString === "PRIVATE"
        ? undefined // For private mode, you would need to configure VPC settings
        : agentcore.RuntimeNetworkConfiguration.usingPublicNetwork()

    // Configure JWT authorizer with Cognito
    const authorizerConfiguration = agentcore.RuntimeAuthorizerConfiguration.usingJWT(
      `https://cognito-idp.${stack.region}.amazonaws.com/${this.userPoolId}/.well-known/openid-configuration`,
      [this.userPoolClientId]
    )

    // Create AgentCore execution role
    const agentRole = new AgentCoreRole(this, "AgentCoreRole")

    // Create memory resource with short-term memory (conversation history) as default
    const memory = new cdk.CfnResource(this, "AgentMemory", {
      type: "AWS::BedrockAgentCore::Memory",
      properties: {
        Name: cdk.Names.uniqueResourceName(this, { maxLength: 48 }),
        EventExpiryDuration: 30,
        Description: `Short-term memory for ${config.stack_name_base} agent`,
        MemoryStrategies: [], // Empty array = short-term only (conversation history)
        MemoryExecutionRoleArn: agentRole.roleArn,
        Tags: {
          Name: `${config.stack_name_base}_Memory`,
          ManagedBy: "CDK",
        },
      },
    })
    const memoryId = memory.getAtt("MemoryId").toString()
    const memoryArn = memory.getAtt("MemoryArn").toString()

    // Store the memory ARN for access from main stack
    this.memoryArn = memoryArn

    // Add memory-specific permissions to agent role
    agentRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "MemoryResourceAccess",
        effect: iam.Effect.ALLOW,
        actions: [
          "bedrock-agentcore:CreateEvent",
          "bedrock-agentcore:GetEvent",
          "bedrock-agentcore:ListEvents",
          "bedrock-agentcore:RetrieveMemoryRecords", // Only needed for long-term strategies
        ],
        resources: [memoryArn],
      })
    )

    // Add SSM permissions for Gateway URL lookup
    agentRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "SSMParameterAccess",
        effect: iam.Effect.ALLOW,
        actions: ["ssm:GetParameter", "ssm:GetParameters"],
        resources: [
          `arn:aws:ssm:${this.region}:${this.account}:parameter/${config.stack_name_base}/*`,
        ],
      })
    )

    // Add Code Interpreter permissions
    agentRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "CodeInterpreterAccess",
        effect: iam.Effect.ALLOW,
        actions: [
          "bedrock-agentcore:StartCodeInterpreterSession",
          "bedrock-agentcore:StopCodeInterpreterSession",
          "bedrock-agentcore:InvokeCodeInterpreter",
        ],
        resources: [`arn:aws:bedrock-agentcore:${this.region}:aws:code-interpreter/*`],
      })
    )

    // Environment variables for the runtime
    const envVars: { [key: string]: string } = {
      AWS_REGION: stack.region,
      AWS_DEFAULT_REGION: stack.region,
      MEMORY_ID: memoryId,
      STACK_NAME: config.stack_name_base, // Required for agent to find SSM parameters
      MODEL_ID: config.backend?.model_id || "global.anthropic.claude-sonnet-4-5-20250929-v1:0",
      STAGING_BUCKET_NAME: this.stagingBucketName, // For S3 report upload
      TOOLS_CONFIG: JSON.stringify(config.tools || {}), // Tool enabled/default_on config
    }

    // Grant S3 write permissions for report upload
    agentRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
        resources: [`arn:aws:s3:::${this.stagingBucketName}/reports/*`],
      })
    )

    // Grant S3 read permissions for user-uploaded content (PDFs, references)
    agentRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ["s3:GetObject"],
        resources: [`arn:aws:s3:::${this.stagingBucketName}/uploads/*`],
      })
    )

    // Create the runtime using L2 construct
    // requestHeaderConfiguration allows the agent to read the Authorization header
    // from RequestContext.request_headers, which is needed to securely extract the
    // user ID from the validated JWT token (sub claim) instead of trusting the payload body.
    this.agentRuntime = new agentcore.Runtime(this, "Runtime", {
      runtimeName: `${config.stack_name_base.replace(/-/g, "_")}_${this.agentName.valueAsString}`,
      agentRuntimeArtifact: agentRuntimeArtifact,
      executionRole: agentRole,
      networkConfiguration: networkConfiguration,
      protocolConfiguration: agentcore.ProtocolType.HTTP,
      environmentVariables: envVars,
      authorizerConfiguration: authorizerConfiguration,
      requestHeaderConfiguration: {
        allowlistedHeaders: ["Authorization"],
      },
      description: `${pattern} agent runtime for ${config.stack_name_base}`,
      lifecycleConfiguration: {
        idleRuntimeSessionTimeout: cdk.Duration.minutes(30),
      },
    })

    // Make sure that ZIP is uploaded before Runtime is created
    if (zipPackagerResource) {
      this.agentRuntime.node.addDependency(zipPackagerResource)
    }

    // Store the runtime ARN
    this.runtimeArn = this.agentRuntime.agentRuntimeArn

    // Outputs
    new cdk.CfnOutput(this, "AgentRuntimeId", {
      description: "ID of the created agent runtime",
      value: this.agentRuntime.agentRuntimeId,
    })

    new cdk.CfnOutput(this, "AgentRuntimeArn", {
      description: "ARN of the created agent runtime",
      value: this.agentRuntime.agentRuntimeArn,
      exportName: `${config.stack_name_base}-AgentRuntimeArn`,
    })

    new cdk.CfnOutput(this, "AgentRoleArn", {
      description: "ARN of the agent execution role",
      value: agentRole.roleArn,
    })

    // Memory ARN output
    new cdk.CfnOutput(this, "MemoryArn", {
      description: "ARN of the agent memory resource",
      value: memoryArn,
    })
  }

  private createRuntimeSSMParameters(config: AppConfig): void {
    // Store runtime ARN in SSM for frontend stack
    new ssm.StringParameter(this, "RuntimeArnParam", {
      parameterName: `/${config.stack_name_base}/runtime-arn`,
      stringValue: this.runtimeArn,
    })
  }

  private createCognitoSSMParameters(config: AppConfig): void {
    // Store Cognito configuration in SSM for testing and frontend access
    new ssm.StringParameter(this, "CognitoUserPoolIdParam", {
      parameterName: `/${config.stack_name_base}/cognito-user-pool-id`,
      stringValue: this.userPoolId,
      description: "Cognito User Pool ID",
    })

    new ssm.StringParameter(this, "CognitoUserPoolClientIdParam", {
      parameterName: `/${config.stack_name_base}/cognito-user-pool-client-id`,
      stringValue: this.userPoolClientId,
      description: "Cognito User Pool Client ID",
    })

    new ssm.StringParameter(this, "MachineClientIdParam", {
      parameterName: `/${config.stack_name_base}/machine_client_id`,
      stringValue: this.machineClient.userPoolClientId,
      description: "Machine Client ID for M2M authentication",
    })

    new secretsmanager.Secret(this, "MachineClientSecret", {
      secretName: `/${config.stack_name_base}/machine_client_secret`,
      secretStringValue: cdk.SecretValue.unsafePlainText(
        this.machineClient.userPoolClientSecret.unsafeUnwrap()
      ),
      description: "Machine Client Secret for M2M authentication",
    })

    // Use the correct Cognito domain format from the passed domain
    new ssm.StringParameter(this, "CognitoDomainParam", {
      parameterName: `/${config.stack_name_base}/cognito_provider`,
      stringValue: `${this.userPoolDomain.domainName}.auth.${cdk.Aws.REGION}.amazoncognito.com`,
      description: "Cognito domain URL for token endpoint",
    })
  }

  // Creates a DynamoDB table for storing user feedback.
  private createFeedbackTable(config: AppConfig): dynamodb.Table {
    const feedbackTable = new dynamodb.Table(this, "FeedbackTable", {
      tableName: `${config.stack_name_base}-feedback`,
      partitionKey: {
        name: "feedbackId",
        type: dynamodb.AttributeType.STRING,
      },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      pointInTimeRecoverySpecification: {
        pointInTimeRecoveryEnabled: true,
      },
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
    })

    // Add GSI for querying by feedbackType with timestamp sorting
    feedbackTable.addGlobalSecondaryIndex({
      indexName: "feedbackType-timestamp-index",
      partitionKey: {
        name: "feedbackType",
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: "timestamp",
        type: dynamodb.AttributeType.NUMBER,
      },
      projectionType: dynamodb.ProjectionType.ALL,
    })

    return feedbackTable
  }

  /**
   * Creates an API Gateway with Lambda integration for the feedback endpoint.
   * This is an EXAMPLE implementation demonstrating best practices for API Gateway + Lambda.
   *
   * API Contract - POST /feedback
   * Authorization: Bearer <cognito-access-token> (required)
   *
   * Request Body:
   *   sessionId: string (required, max 100 chars, alphanumeric with -_) - Conversation session ID
   *   message: string (required, max 5000 chars) - Agent's response being rated
   *   feedbackType: "positive" | "negative" (required) - User's rating
   *   comment: string (optional, max 5000 chars) - User's explanation for rating
   *
   * Success Response (200):
   *   { success: true, feedbackId: string }
   *
   * Error Responses:
   *   400: { error: string } - Validation failure (missing fields, invalid format)
   *   401: { error: "Unauthorized" } - Invalid/missing JWT token
   *   500: { error: "Internal server error" } - DynamoDB or processing error
   *
   * Implementation: infra-cdk/lambdas/feedback/index.py
   */
  private createFeedbackApi(
    config: AppConfig,
    frontendUrl: string,
    feedbackTable: dynamodb.Table
  ): void {
    // Create Lambda function for feedback using Python
    const feedbackLambda = new PythonFunction(this, "FeedbackLambda", {
      functionName: `${config.stack_name_base}-feedback`,
      runtime: lambda.Runtime.PYTHON_3_13,
      tracing: lambda.Tracing.ACTIVE,
      entry: path.join(__dirname, "..", "lambdas", "feedback"),
      handler: "handler",
      environment: {
        TABLE_NAME: feedbackTable.tableName,
        CORS_ALLOWED_ORIGINS: `${frontendUrl},http://localhost:3000`,
      },
      timeout: cdk.Duration.seconds(30),
      layers: [
        lambda.LayerVersion.fromLayerVersionArn(
          this,
          "PowertoolsLayer",
          `arn:aws:lambda:${
            cdk.Stack.of(this).region
          }:017000801446:layer:AWSLambdaPowertoolsPythonV3-python313-arm64:18`
        ),
      ],
      logGroup: new logs.LogGroup(this, "FeedbackLambdaLogGroup", {
        logGroupName: `/aws/lambda/${config.stack_name_base}-feedback`,
        retention: logs.RetentionDays.ONE_WEEK,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      }),
    })

    // Grant Lambda permissions to write to DynamoDB
    feedbackTable.grantWriteData(feedbackLambda)

    this.addLambdaAlarms(feedbackLambda, "Feedback")

    /*
     * CORS TODO: Wildcard (*) used because Backend deploys before Frontend in nested stack order.
     * For Lambda proxy integrations, the Lambda's ALLOWED_ORIGINS env var is the primary CORS control.
     * API Gateway defaultCorsPreflightOptions below only handles OPTIONS preflight requests.
     * See detailed explanation and fix options in: infra-cdk/lambdas/feedback/index.py
     */
    const api = new apigateway.RestApi(this, "FeedbackApi", {
      restApiName: `${config.stack_name_base}-api`,
      description: "API for user feedback and future endpoints",
      defaultCorsPreflightOptions: {
        allowOrigins: [frontendUrl, "http://localhost:3000"],
        allowMethods: ["POST", "OPTIONS"],
        allowHeaders: ["Content-Type", "Authorization"],
      },
      deployOptions: {
        stageName: "prod",
        cacheClusterEnabled: true,
        cacheClusterSize: "0.5",
        // All method-level settings (cache, logging, metrics, throttling) go
        // through methodOptions so CFN emits a single MethodSettings entry per
        // resource path. Scanners flag stages where the first MethodSettings
        // entry is missing any one of these controls.
        methodOptions: {
          "/*/*": {
            cachingEnabled: true,
            cacheTtl: cdk.Duration.minutes(5),
            cacheDataEncrypted: true,
            loggingLevel: apigateway.MethodLoggingLevel.INFO,
            dataTraceEnabled: true,
            metricsEnabled: true,
            throttlingRateLimit: 100,
            throttlingBurstLimit: 200,
          },
        },
        accessLogDestination: new apigateway.LogGroupLogDestination(
          new logs.LogGroup(this, "FeedbackApiAccessLogGroup", {
            logGroupName: `/aws/apigateway/${config.stack_name_base}-api-access`,
            retention: logs.RetentionDays.ONE_WEEK,
            removalPolicy: cdk.RemovalPolicy.DESTROY,
          })
        ),
        accessLogFormat: apigateway.AccessLogFormat.jsonWithStandardFields(),
        tracingEnabled: true,
      },
    })

    // Add request validator for API security
    const requestValidator = new apigateway.RequestValidator(this, "FeedbackApiRequestValidator", {
      restApi: api,
      requestValidatorName: `${config.stack_name_base}-request-validator`,
      validateRequestBody: true,
      validateRequestParameters: true,
    })

    // Create Cognito authorizer
    const authorizer = new apigateway.CognitoUserPoolsAuthorizer(this, "FeedbackApiAuthorizer", {
      cognitoUserPools: [this.userPool],
      identitySource: "method.request.header.Authorization",
      authorizerName: `${config.stack_name_base}-authorizer`,
    })

    // Create /feedback resource and POST method
    const feedbackResource = api.root.addResource("feedback")
    feedbackResource.addMethod("POST", new apigateway.LambdaIntegration(feedbackLambda), {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
      requestValidator: requestValidator,
    })

    // Create upload Lambda for pre-signed S3 URLs
    const uploadLambda = new PythonFunction(this, "UploadLambda", {
      functionName: `${config.stack_name_base}-upload`,
      runtime: lambda.Runtime.PYTHON_3_13,
      tracing: lambda.Tracing.ACTIVE,
      entry: path.join(__dirname, "..", "lambdas", "upload"),
      handler: "handler",
      environment: {
        BUCKET_NAME: this.stagingBucketName,
        CORS_ALLOWED_ORIGINS: `${frontendUrl},http://localhost:3000`,
      },
      timeout: cdk.Duration.seconds(10),
      layers: [
        lambda.LayerVersion.fromLayerVersionArn(
          this,
          "PowertoolsLayerUpload",
          `arn:aws:lambda:${
            cdk.Stack.of(this).region
          }:017000801446:layer:AWSLambdaPowertoolsPythonV3-python313-arm64:18`
        ),
      ],
      logGroup: new logs.LogGroup(this, "UploadLambdaLogGroup", {
        logGroupName: `/aws/lambda/${config.stack_name_base}-upload`,
        retention: logs.RetentionDays.ONE_WEEK,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      }),
    })

    // Grant upload Lambda permission to put objects in staging bucket
    this.stagingBucket.grantPut(uploadLambda)

    this.addLambdaAlarms(uploadLambda, "Upload")

    // Create /upload resource and POST method
    const uploadResource = api.root.addResource("upload")
    uploadResource.addMethod("POST", new apigateway.LambdaIntegration(uploadLambda), {
      authorizer,
      authorizationType: apigateway.AuthorizationType.COGNITO,
      requestValidator: requestValidator,
    })

    // Store the API URL for access from main stack
    this.feedbackApiUrl = api.url

    // Store API URL in SSM for frontend
    new ssm.StringParameter(this, "FeedbackApiUrlParam", {
      parameterName: `/${config.stack_name_base}/feedback-api-url`,
      stringValue: api.url,
      description: "Feedback API Gateway URL",
    })

    // Attach AWS-managed common rule set via WAFv2 to the prod stage.
    const webAcl = new wafv2.CfnWebACL(this, "FeedbackApiWebAcl", {
      name: `${config.stack_name_base}-feedback-api-waf`,
      scope: "REGIONAL",
      defaultAction: { allow: {} },
      visibilityConfig: {
        cloudWatchMetricsEnabled: true,
        metricName: `${config.stack_name_base}-feedback-api-waf`,
        sampledRequestsEnabled: true,
      },
      rules: [
        {
          name: "AWS-AWSManagedRulesCommonRuleSet",
          priority: 0,
          overrideAction: { none: {} },
          statement: {
            managedRuleGroupStatement: {
              vendorName: "AWS",
              name: "AWSManagedRulesCommonRuleSet",
            },
          },
          visibilityConfig: {
            cloudWatchMetricsEnabled: true,
            metricName: "AWSManagedRulesCommonRuleSet",
            sampledRequestsEnabled: true,
          },
        },
        {
          // Includes the Log4j/Log4Shell (CVE-2021-44228) rule among other
          // known-bad input patterns.
          name: "AWS-AWSManagedRulesKnownBadInputsRuleSet",
          priority: 1,
          overrideAction: { none: {} },
          statement: {
            managedRuleGroupStatement: {
              vendorName: "AWS",
              name: "AWSManagedRulesKnownBadInputsRuleSet",
            },
          },
          visibilityConfig: {
            cloudWatchMetricsEnabled: true,
            metricName: "AWSManagedRulesKnownBadInputsRuleSet",
            sampledRequestsEnabled: true,
          },
        },
      ],
    })

    const stageArn = cdk.Stack.of(this).formatArn({
      service: "apigateway",
      account: "",
      resource: `/restapis/${api.restApiId}/stages/${api.deploymentStage.stageName}`,
    })

    new wafv2.CfnWebACLAssociation(this, "FeedbackApiWebAclAssociation", {
      resourceArn: stageArn,
      webAclArn: webAcl.attrArn,
    })
  }

  private createAgentCoreGateway(config: AppConfig): void {
    // Create sample tool Lambda
    const toolLambda = new lambda.Function(this, "SampleToolLambda", {
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: "sample_tool_lambda.handler",
      code: lambda.Code.fromAsset(path.join(__dirname, "../../gateway/tools/sample_tool")),
      timeout: cdk.Duration.seconds(30),
      tracing: lambda.Tracing.ACTIVE,
      logGroup: new logs.LogGroup(this, "SampleToolLambdaLogGroup", {
        logGroupName: `/aws/lambda/${config.stack_name_base}-sample-tool`,
        retention: logs.RetentionDays.ONE_WEEK,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      }),
    })

    this.addLambdaAlarms(toolLambda, "SampleTool")

    // ========== GATEWAY TOOLS ==========

    // Helper to check if a tool is enabled in config
    const isToolEnabled = (toolId: string): boolean => config.tools?.[toolId]?.enabled !== false

    // Nova Web Search Lambda
    let novaSearchLambda: lambda.Function | undefined
    if (isToolEnabled("nova")) {
      novaSearchLambda = new lambda.Function(this, "NovaSearchLambda", {
        runtime: lambda.Runtime.PYTHON_3_13,
        handler: "nova_search_lambda.handler",
        code: lambda.Code.fromAsset(path.join(__dirname, "../../gateway/tools/nova_search"), {
          bundling: this.getPythonBundlingOptions(["boto3"]),
        }),
        timeout: cdk.Duration.minutes(2),
        logGroup: new logs.LogGroup(this, "NovaSearchLambdaLogGroup", {
          logGroupName: `/aws/lambda/${config.stack_name_base}-nova-search`,
          retention: logs.RetentionDays.ONE_WEEK,
          removalPolicy: cdk.RemovalPolicy.DESTROY,
        }),
      })

      // Grant Nova Search Lambda access to Bedrock and Web Grounding
      novaSearchLambda.addToRolePolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: ["bedrock:InvokeModel", "bedrock:Converse", "bedrock:InvokeTool"],
          resources: [
            "arn:aws:bedrock:*::foundation-model/*",
            `arn:aws:bedrock:${this.region}:${this.account}:inference-profile/*`,
            // System tool ARN has no region (AWS-managed)
            `arn:aws:bedrock::${this.account}:system-tool/amazon.nova_grounding`,
          ],
        })
      )

      this.addLambdaAlarms(novaSearchLambda, "NovaSearch")
    }

    // OpenFDA Drug Search Lambda
    let openfdaLambda: lambda.Function | undefined
    if (isToolEnabled("openfda")) {
      openfdaLambda = new lambda.Function(this, "OpenFDASearchLambda", {
        runtime: lambda.Runtime.PYTHON_3_13,
        handler: "openfda_lambda.handler",
        code: lambda.Code.fromAsset(path.join(__dirname, "../../gateway/tools/openfda"), {
          bundling: this.getPythonBundlingOptions([]),
        }),
        timeout: cdk.Duration.seconds(60),
        logGroup: new logs.LogGroup(this, "OpenFDALambdaLogGroup", {
          logGroupName: `/aws/lambda/${config.stack_name_base}-openfda-search`,
          retention: logs.RetentionDays.ONE_WEEK,
          removalPolicy: cdk.RemovalPolicy.DESTROY,
        }),
      })

      this.addLambdaAlarms(openfdaLambda, "OpenFDASearch")
    }

    // PubMed Search Lambda
    let pubmedLambda: lambda.Function | undefined
    if (isToolEnabled("pubmed")) {
      pubmedLambda = new lambda.Function(this, "PubMedSearchLambda", {
        runtime: lambda.Runtime.PYTHON_3_13,
        handler: "pubmed_search_lambda.handler",
        code: lambda.Code.fromAsset(path.join(__dirname, "../../gateway/tools/pubmed_search"), {
          bundling: this.getPythonBundlingOptions([]),
        }),
        timeout: cdk.Duration.seconds(60),
        logGroup: new logs.LogGroup(this, "PubMedLambdaLogGroup", {
          logGroupName: `/aws/lambda/${config.stack_name_base}-pubmed-search`,
          retention: logs.RetentionDays.ONE_WEEK,
          removalPolicy: cdk.RemovalPolicy.DESTROY,
        }),
      })

      this.addLambdaAlarms(pubmedLambda, "PubMedSearch")
    }

    // ClinicalTrials.gov Search Lambda
    let clinicaltrialsLambda: lambda.Function | undefined
    if (isToolEnabled("clinicaltrials")) {
      clinicaltrialsLambda = new lambda.Function(this, "ClinicalTrialsSearchLambda", {
        runtime: lambda.Runtime.PYTHON_3_13,
        handler: "clinicaltrials_search_lambda.handler",
        code: lambda.Code.fromAsset(
          path.join(__dirname, "../../gateway/tools/clinicaltrials_search"),
          {
            bundling: this.getPythonBundlingOptions([]),
          }
        ),
        timeout: cdk.Duration.seconds(60),
        logGroup: new logs.LogGroup(this, "ClinicalTrialsLambdaLogGroup", {
          logGroupName: `/aws/lambda/${config.stack_name_base}-clinicaltrials-search`,
          retention: logs.RetentionDays.ONE_WEEK,
          removalPolicy: cdk.RemovalPolicy.DESTROY,
        }),
      })

      this.addLambdaAlarms(clinicaltrialsLambda, "ClinicalTrialsSearch")
    }

    // ========== END GATEWAY TOOLS ==========

    // Create comprehensive IAM role for gateway
    const gatewayRole = new iam.Role(this, "GatewayRole", {
      assumedBy: new iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
      description: "Role for AgentCore Gateway with comprehensive permissions",
    })

    // Lambda invoke permissions for gateway-backed tools
    toolLambda.grantInvoke(gatewayRole)
    if (novaSearchLambda) novaSearchLambda.grantInvoke(gatewayRole)
    if (openfdaLambda) openfdaLambda.grantInvoke(gatewayRole)
    if (pubmedLambda) pubmedLambda.grantInvoke(gatewayRole)
    if (clinicaltrialsLambda) clinicaltrialsLambda.grantInvoke(gatewayRole)

    // Bedrock permissions (region-agnostic)
    gatewayRole.addToPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
        resources: [
          "arn:aws:bedrock:*::foundation-model/*",
          `arn:aws:bedrock:*:${this.account}:inference-profile/*`,
        ],
      })
    )

    // SSM parameter access
    gatewayRole.addToPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ["ssm:GetParameter", "ssm:GetParameters"],
        resources: [
          `arn:aws:ssm:${this.region}:${this.account}:parameter/${config.stack_name_base}/*`,
        ],
      })
    )

    // Cognito permissions
    gatewayRole.addToPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ["cognito-idp:DescribeUserPoolClient", "cognito-idp:InitiateAuth"],
        resources: [this.userPool.userPoolArn],
      })
    )

    // CloudWatch Logs
    gatewayRole.addToPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
        resources: [
          `arn:aws:logs:${this.region}:${this.account}:log-group:/aws/bedrock-agentcore/*`,
        ],
      })
    )

    // Load tool specification from JSON file
    const toolSpecPath = path.join(__dirname, "../../gateway/tools/sample_tool/tool_spec.json")
    const apiSpec = JSON.parse(require("fs").readFileSync(toolSpecPath, "utf8"))

    // Cognito OAuth2 configuration for gateway
    const cognitoIssuer = `https://cognito-idp.${this.region}.amazonaws.com/${this.userPool.userPoolId}`
    const cognitoDiscoveryUrl = `${cognitoIssuer}/.well-known/openid-configuration`

    // Create Gateway using L1 construct (CfnGateway)
    // This replaces the Custom Resource approach with native CloudFormation support
    const gateway = new bedrockagentcore.CfnGateway(this, "AgentCoreGateway", {
      name: `${config.stack_name_base}-gateway`,
      roleArn: gatewayRole.roleArn,
      protocolType: "MCP",
      protocolConfiguration: {
        mcp: {
          supportedVersions: ["2025-03-26"],
          // Optional: Enable semantic search for tools
          // searchType: "SEMANTIC",
        },
      },
      authorizerType: "CUSTOM_JWT",
      authorizerConfiguration: {
        customJwtAuthorizer: {
          allowedClients: [this.machineClient.userPoolClientId],
          discoveryUrl: cognitoDiscoveryUrl,
        },
      },
      description: "AgentCore Gateway with MCP protocol and JWT authentication",
    })

    // Create Gateway Target using L1 construct (CfnGatewayTarget)
    const gatewayTarget = new bedrockagentcore.CfnGatewayTarget(this, "GatewayTarget", {
      gatewayIdentifier: gateway.attrGatewayIdentifier,
      name: "sample-tool-target",
      description: "Sample tool Lambda target",
      targetConfiguration: {
        mcp: {
          lambda: {
            lambdaArn: toolLambda.functionArn,
            toolSchema: {
              inlinePayload: apiSpec,
            },
          },
        },
      },
      credentialProviderConfigurations: [
        {
          credentialProviderType: "GATEWAY_IAM_ROLE",
        },
      ],
    })

    // ========== GATEWAY TOOL TARGETS ==========

    // Helper to load tool spec and create gateway target
    // nosemgrep: path-join-resolve-traversal -- build-time spec loading from known paths
    const loadToolSpec = (specPath: string) =>
      JSON.parse(fs.readFileSync(path.join(__dirname, specPath), "utf8"))

    const createGatewayTarget = (
      id: string,
      name: string,
      description: string,
      toolLambdaFn: lambda.Function,
      specPath: string
    ) => {
      const spec = loadToolSpec(specPath)
      const target = new bedrockagentcore.CfnGatewayTarget(this, id, {
        gatewayIdentifier: gateway.attrGatewayIdentifier,
        name,
        description,
        targetConfiguration: {
          mcp: {
            lambda: {
              lambdaArn: toolLambdaFn.functionArn,
              toolSchema: { inlinePayload: spec },
            },
          },
        },
        credentialProviderConfigurations: [{ credentialProviderType: "GATEWAY_IAM_ROLE" }],
      })
      target.addDependency(gateway)
      return target
    }

    if (novaSearchLambda) {
      createGatewayTarget(
        "NovaSearchTarget",
        "nova-search-target",
        "Nova web search with grounding",
        novaSearchLambda,
        "../../gateway/tools/nova_search/tool_spec.json"
      )
    }

    if (openfdaLambda) {
      createGatewayTarget(
        "OpenFDASearchTarget",
        "openfda-search-target",
        "OpenFDA drug label search",
        openfdaLambda,
        "../../gateway/tools/openfda/tool_spec.json"
      )
    }

    if (pubmedLambda) {
      createGatewayTarget(
        "PubMedSearchTarget",
        "pubmed-search-target",
        "PubMed biomedical literature search",
        pubmedLambda,
        "../../gateway/tools/pubmed_search/tool_spec.json"
      )
    }

    if (clinicaltrialsLambda) {
      createGatewayTarget(
        "ClinicalTrialsSearchTarget",
        "clinicaltrials-search-target",
        "ClinicalTrials.gov clinical study search",
        clinicaltrialsLambda,
        "../../gateway/tools/clinicaltrials_search/tool_spec.json"
      )
    }

    // ========== END GATEWAY TOOL TARGETS ==========

    // Ensure proper creation order
    gatewayTarget.addDependency(gateway)
    gateway.node.addDependency(toolLambda)
    if (novaSearchLambda) gateway.node.addDependency(novaSearchLambda)
    if (openfdaLambda) gateway.node.addDependency(openfdaLambda)
    if (pubmedLambda) gateway.node.addDependency(pubmedLambda)
    if (clinicaltrialsLambda) gateway.node.addDependency(clinicaltrialsLambda)
    gateway.node.addDependency(this.machineClient)
    gateway.node.addDependency(gatewayRole)

    // Store Gateway URL in SSM for runtime access
    new ssm.StringParameter(this, "GatewayUrlParam", {
      parameterName: `/${config.stack_name_base}/gateway_url`,
      stringValue: gateway.attrGatewayUrl,
      description: "AgentCore Gateway URL",
    })

    // Output gateway information
    new cdk.CfnOutput(this, "GatewayId", {
      value: gateway.attrGatewayIdentifier,
      description: "AgentCore Gateway ID",
    })

    new cdk.CfnOutput(this, "GatewayUrl", {
      value: gateway.attrGatewayUrl,
      description: "AgentCore Gateway URL",
    })

    new cdk.CfnOutput(this, "GatewayArn", {
      value: gateway.attrGatewayArn,
      description: "AgentCore Gateway ARN",
    })

    new cdk.CfnOutput(this, "GatewayTargetId", {
      value: gatewayTarget.ref,
      description: "AgentCore Gateway Target ID",
    })

    new cdk.CfnOutput(this, "ToolLambdaArn", {
      description: "ARN of the sample tool Lambda",
      value: toolLambda.functionArn,
    })
  }

  private createMachineAuthentication(config: AppConfig): void {
    // Create Resource Server for Machine-to-Machine (M2M) authentication
    // This defines the API scopes that machine clients can request access to
    const resourceServer = new cognito.UserPoolResourceServer(this, "ResourceServer", {
      userPool: this.userPool,
      identifier: `${config.stack_name_base}-gateway`,
      userPoolResourceServerName: `${config.stack_name_base}-gateway-resource-server`,
      scopes: [
        new cognito.ResourceServerScope({
          scopeName: "read",
          scopeDescription: "Read access to gateway",
        }),
        new cognito.ResourceServerScope({
          scopeName: "write",
          scopeDescription: "Write access to gateway",
        }),
      ],
    })

    // Create Machine Client for AgentCore Gateway authentication
    //
    // WHAT IS A MACHINE CLIENT?
    // A machine client is a Cognito User Pool Client configured for server-to-server authentication
    // using the OAuth2 Client Credentials flow. Unlike user-facing clients, it doesn't require
    // human interaction or user credentials.
    //
    // HOW IS IT DIFFERENT FROM THE REGULAR USER POOL CLIENT?
    // - Regular client: Uses Authorization Code flow for human users (frontend login)
    // - Machine client: Uses Client Credentials flow for service-to-service authentication
    // - Regular client: No client secret (public client for frontend security)
    // - Machine client: Has client secret (confidential client for backend security)
    // - Regular client: Scopes are openid, email, profile (user identity)
    // - Machine client: Scopes are custom resource server scopes (API permissions)
    //
    // WHY IS IT NEEDED?
    // The AgentCore Gateway needs to authenticate with Cognito to validate tokens and make
    // API calls on behalf of the system. The machine client provides the credentials for
    // this service-to-service authentication without requiring user interaction.
    this.machineClient = new cognito.UserPoolClient(this, "MachineClient", {
      userPool: this.userPool,
      userPoolClientName: `${config.stack_name_base}-machine-client`,
      generateSecret: true, // Required for client credentials flow
      oAuth: {
        flows: {
          clientCredentials: true, // Enable OAuth2 Client Credentials flow
        },
        scopes: [
          // Grant access to the resource server scopes defined above
          cognito.OAuthScope.resourceServer(
            resourceServer,
            new cognito.ResourceServerScope({
              scopeName: "read",
              scopeDescription: "Read access to gateway",
            })
          ),
          cognito.OAuthScope.resourceServer(
            resourceServer,
            new cognito.ResourceServerScope({
              scopeName: "write",
              scopeDescription: "Write access to gateway",
            })
          ),
        ],
      },
    })

    // Machine client must be created after resource server
    this.machineClient.node.addDependency(resourceServer)
  }

  /**
   * Track which Lambda construct paths already have alarms wired via
   * addLambdaAlarms so the Aspect can skip them.
   */
  private alarmedLambdaPaths: Set<string> = new Set<string>()

  /**
   * Attach a minimal Errors alarm to any Lambda CFN resource that doesn't
   * already have one wired via addLambdaAlarms. Used from the Aspect to cover
   * CDK-generated singleton Lambdas (custom-resource providers, s3
   * autoDeleteObjects handlers) that scanners still expect to be monitored.
   */
  private ensureCfnLambdaErrorsAlarm(node: cdk.CfnResource): void {
    // The CFN resource lives under the L2 Lambda construct's path; checking
    // the parent path lets us skip anything already covered by addLambdaAlarms.
    const parentPath = node.node.scope?.node.path
    if (parentPath && this.alarmedLambdaPaths.has(parentPath)) {
      return
    }
    const logicalId = cdk.Stack.of(node).resolve(node.logicalId) as string
    const alarm = new cloudwatch.Alarm(this, `${logicalId}AutoErrorsAlarm`, {
      metric: new cloudwatch.Metric({
        namespace: "AWS/Lambda",
        metricName: "Errors",
        // For AWS::Lambda::Function, Ref returns the function name.
        dimensionsMap: { FunctionName: node.ref },
        statistic: "Sum",
        period: cdk.Duration.minutes(5),
      }),
      threshold: 1,
      evaluationPeriods: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    })
    alarm.addAlarmAction(new cw_actions.SnsAction(this.alarmTopic))
  }

  /**
   * Create standard CloudWatch alarms for a Lambda function. Publishes to the
   * shared alarmTopic. Covers Errors, Throttles, Duration, Invocations,
   * ConcurrentExecutions, and DeadLetterErrors.
   */
  private addLambdaAlarms(fn: lambda.IFunction, idPrefix: string): void {
    this.alarmedLambdaPaths.add(fn.node.path)
    const action = new cw_actions.SnsAction(this.alarmTopic)

    const mk = (
      id: string,
      metric: cloudwatch.Metric,
      threshold: number,
      evaluationPeriods = 1
    ): void => {
      const alarm = new cloudwatch.Alarm(this, `${idPrefix}${id}Alarm`, {
        metric,
        threshold,
        evaluationPeriods,
        comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
        treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      })
      alarm.addAlarmAction(action)
    }

    mk("Errors", fn.metricErrors({ period: cdk.Duration.minutes(5) }), 1)
    mk("Throttles", fn.metricThrottles({ period: cdk.Duration.minutes(5) }), 1)
    mk("Duration", fn.metricDuration({ period: cdk.Duration.minutes(5), statistic: "p95" }), 10_000)
    mk("Invocations", fn.metricInvocations({ period: cdk.Duration.minutes(5) }), 10_000)
    mk(
      "ConcurrentExecutions",
      new cloudwatch.Metric({
        namespace: "AWS/Lambda",
        metricName: "ConcurrentExecutions",
        dimensionsMap: { FunctionName: fn.functionName },
        statistic: "Maximum",
        period: cdk.Duration.minutes(5),
      }),
      100
    )
    mk(
      "DeadLetterErrors",
      new cloudwatch.Metric({
        namespace: "AWS/Lambda",
        metricName: "DeadLetterErrors",
        dimensionsMap: { FunctionName: fn.functionName },
        statistic: "Sum",
        period: cdk.Duration.minutes(5),
      }),
      1
    )
  }

  /**
   * Recursively read directory contents and encode as base64.
   *
   * @param dirPath - Directory to read.
   * @param prefix - Prefix for file paths in output.
   * @param output - Output object to populate.
   */
  private readDirRecursive(dirPath: string, prefix: string, output: Record<string, string>): void {
    for (const entry of fs.readdirSync(dirPath, { withFileTypes: true })) {
      // nosemgrep: path-join-resolve-traversal -- build-time directory traversal
      const fullPath = path.join(dirPath, entry.name)
      const relativePath = path.join(prefix, entry.name)

      if (entry.isDirectory()) {
        // Skip __pycache__ directories
        if (entry.name !== "__pycache__") {
          this.readDirRecursive(fullPath, relativePath, output)
        }
      } else if (entry.isFile()) {
        const content = fs.readFileSync(fullPath)
        output[relativePath] = content.toString("base64")
      }
    }
  }

  /**
   * Create a hash of content for change detection.
   *
   * @param content - Content to hash.
   * @returns Hash string.
   */
  private hashContent(content: string): string {
    const crypto = require("crypto")
    return crypto.createHash("sha256").update(content).digest("hex").slice(0, 16)
  }

  /**
   * Get Python bundling options for Lambda functions with pip dependencies.
   *
   * @param dependencies - List of pip packages to install.
   * @returns Bundling options for CDK.
   */
  private getPythonBundlingOptions(dependencies: string[]): cdk.BundlingOptions {
    const commands = []
    if (dependencies.length > 0) {
      commands.push("pip install " + dependencies.join(" ") + " -t /asset-output")
    }
    commands.push("cp -r . /asset-output")

    return {
      image: lambda.Runtime.PYTHON_3_13.bundlingImage,
      command: ["bash", "-c", commands.join(" && ")],
    }
  }
}
