# Infrastructure

This directory contains the AWS CDK infrastructure code for deploying the solution.

## Prerequisites

- Node.js 20+
- AWS CLI configured with appropriate credentials
- AWS CDK CLI installed: `npm install -g aws-cdk`
- Docker installed and running (required for Lambda Docker bundling and the agent container build)

## Minimal IAM Policy for Deployment

The file `minimal-deploy-policy.json` contains the minimum IAM permissions required to deploy this CDK application. This policy includes 30 actions across 7 statements covering CloudFormation, S3, SSM, ECR, IAM PassRole, and Amplify.

**Important:** This policy assumes CDK bootstrap has already been run in the target account. It does not include permissions for `cdk bootstrap`. To bootstrap a fresh account, you'll need additional IAM permissions (CreateRole, AttachRolePolicy, PutRolePolicy, etc.) - refer to the AWS CDK Bootstrap documentation for details.

**Security Note:** Some wildcards are present for resources (e.g., `arn:aws:cloudformation:*:*:stack/*`). For production environments, replace these with your specific resource ARNs to further scope down permissions.

## Getting Started

All of the following commands assuming you are in the top of the `infra-cdk/` directory
### Install Dependencies

```bash
npm install
```

### Build TypeScript

```bash
npm run build
```

### Bootstrap CDK (First Time Only)

```bash
npx cdk bootstrap
```

### Deploy

```bash
npx cdk deploy --all
```

## Useful Commands

* `npm run build`   - Compile TypeScript to JavaScript
* `npm run watch`   - Watch for changes and compile automatically
* `npm run test`    - Run Jest unit tests
* `npx cdk deploy --all` - Deploy all stacks to your AWS account/region
* `npx cdk diff`    - Compare deployed stack with current state
* `npx cdk synth`   - Emit the synthesized CloudFormation template
* `npx cdk destroy --all` - Remove all deployed resources

## Configuration

Edit `config.yaml` to customize your deployment:

```yaml
stack_name_base: medical-content-review
region: null  # AWS region (e.g., us-west-2). If null, uses AWS CLI default region.

admin_user_email: null  # Optional: auto-create an admin Cognito user and email credentials

backend:
  pattern: medical-content-review
  deployment_type: docker  # docker (default) or zip
  model_id: global.anthropic.claude-sonnet-4-6

# Reference-verification tools: enabled = deployed, default_on = toggled on in UI by default
tools:
  pubmed:
    enabled: true
    default_on: true
  openfda:
    enabled: true
    default_on: true
  clinicaltrials:
    enabled: true
    default_on: true
  s3:
    enabled: true
    default_on: true
  nova:
    enabled: true
    default_on: false
  bedrock_kb:
    enabled: false
    default_on: false
    required:
      knowledge_base_id: null
  ...

```

## Project Structure

```
infra-cdk/
├── bin/
│   └── adr-cdk.ts              # CDK app entry point
├── lib/
│   ├── adr-main-stack.ts       # Main orchestrator stack
│   ├── cognito-stack.ts        # Cognito User Pool nested stack
│   ├── backend-stack.ts        # Backend nested stack (AgentCore, Gateway, Lambdas, APIs)
│   ├── amplify-hosting-stack.ts # Amplify Hosting nested stack for the frontend
│   └── utils/                  # Utility functions (config-manager, agentcore-role)
├── lambdas/                    # Lambda function code (feedback, upload, zip-packager)
├── scripts/
│   └── post-deploy.js          # Runs after cdk deploy (triggers frontend deploy)
├── test/
│   └── adr-cdk.test.ts         # Unit tests
├── cdk.json                    # CDK configuration
├── config.yaml                 # Deployment configuration (gitignored)
├── .config_example.yaml        # Example configuration (tracked)
├── minimal-deploy-policy.json  # Minimum IAM policy for deployment
├── package.json
└── tsconfig.json
```

## Development Workflow

1. Make changes to TypeScript files in `lib/`
2. Run `npm run build` to compile
3. Run `npx cdk diff` to see what will change
4. Run `npx cdk deploy --all` to deploy changes

For faster iteration, use watch mode:
```bash
npm run watch
```

## Deployment Details

The CDK deployment is orchestrated by `ADRMainStack`, which instantiates three nested stacks. The construction order inside `adr-main-stack.ts` is:

### Stack Architecture & Deployment Order

1. **Amplify Hosting Stack** (`AmplifyHostingStack`): created first so its predictable Amplify URL is available to downstream stacks. Creates the staging S3 bucket and the Amplify app/branch.
2. **Cognito Stack** (`CognitoStack`): User Pool, User Pool Client, and User Pool Domain. Callback URLs include both `http://localhost:3000` (for local development) and the Amplify URL.
3. **Backend Stack** (`BackendStack`): AgentCore Runtime + Memory + Gateway, tool Lambdas, feedback API + DynamoDB table, upload Lambda, SSM parameters, and Secrets Manager entries. Imports Cognito and Amplify resources from the other stacks.

### Component Dependencies

Within the Backend Stack, components are created in this order:
1. **Cognito Integration**: Import User Pool and User Pool Client from the Cognito stack
2. **Machine Client**: Create OAuth2 client for M2M authentication
3. **Gateway**: Create AgentCore Gateway (depends on machine client) and register enabled tool Lambdas as targets
4. **Runtime**: Create AgentCore Runtime (independent of gateway)

This order ensures authentication components are available before services that depend on them, while keeping the runtime deployment separate since it doesn't directly depend on the gateway.

### Docker Build Configuration

The agent container builds use a specific configuration to handle the repository structure efficiently:

#### Build Context Strategy

**Problem**: Agent patterns need access to the shared `gateway/` utilities package, but Docker build contexts cannot access parent directories using `../` paths.

**Solution**: Use repository root as build context with optimized file filtering:

1. **Build Context**: Repository root (`/path/to/medical-content-review/`)
2. **Dockerfile Location**: `patterns/{pattern}/Dockerfile`
3. **Package Installation**: Install package (`gateway/` + `pyproject.toml`) as proper Python package
4. **File Filtering**: `.dockerignore` excludes large directories to prevent build hangs

#### Docker Context Optimization

**Issue**: Large build contexts (including `node_modules/`, `.git/`, etc.) cause Docker builds to hang during the "transferring context" phase, especially in CDK deployments.

**Solution**: `.dockerignore` file at repository root excludes:
- `node_modules/` directories (frontend and infra)
- `.git/` version control data
- Build artifacts (`cdk.out/`, `.next/`, `dist/`)
- Cache directories (`.ruff_cache/`, `__pycache__/`)

**Result**: Build context reduced from ~100MB+ to ~10MB, eliminating hang issues.

#### Package-Based Architecture

Instead of copying files with relative paths, the Dockerfile:

1. **Installs package**: `RUN pip install --no-cache-dir -e .`
   - Makes `gateway` utilities available as `from gateway.utils.*`
   - Eliminates need for file copying between directories
   - Works consistently across all agent patterns

2. **Copies only agent code**: `COPY patterns/medical-content-review/medical_review_agent.py .`
   - Minimal file copying for the specific agent
   - Clean separation between shared utilities and agent logic

3. **Removes problematic requirements**: Cleaned `requirements.txt` to avoid duplicate installation

This approach maintains clean Docker builds with clear separation between shared utilities and agent logic.

### Key Resources Created

1. **Cognito Stack**:
   - User Pool for end-user sign-in
   - User Pool Client for the frontend OAuth flow
   - User Pool Domain for the hosted UI
   - (Optional) auto-created admin user when `admin_user_email` is set

2. **Backend Stack**:
   - Cognito machine client + resource server for M2M authentication
   - AgentCore Gateway with Lambda tool targets (one Lambda per enabled tool)
   - AgentCore Runtime (Docker or zip packaging) and AgentCore Memory
   - Feedback API Gateway + DynamoDB table
   - Upload Lambda for pre-signed S3 URLs
   - SSM parameters and Secrets Manager entries for runtime configuration

3. **Amplify Hosting Stack**:
   - S3 staging bucket (with access logging) used for deployment artifacts and file uploads/reports
   - Amplify Hosting app and branch for the React frontend

## Troubleshooting

### Build Errors

If you encounter TypeScript compilation errors:
```bash
npm run build
```

### Deployment Failures

Check CloudFormation events in the AWS Console for detailed error messages.

### Clean Build

If you need to start fresh:
```bash
rm -rf node_modules cdk.out
npm install
npm run build
```

## Testing

Run unit tests:
```bash
npm test
```

## Learn More

- [AWS CDK Documentation](https://docs.aws.amazon.com/cdk/)
- [AWS CDK TypeScript Reference](https://docs.aws.amazon.com/cdk/api/v2/docs/aws-construct-library.html)
- [Bedrock AgentCore Documentation](https://docs.aws.amazon.com/bedrock/)
