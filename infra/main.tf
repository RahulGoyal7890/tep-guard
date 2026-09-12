terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project   = var.project
      ManagedBy = "terraform"
      # Makes it obvious in Cost Explorer which resources are this project,
      # and easy to find anything that survives a destroy.
      Ephemeral = "true"
    }
  }
}

data "aws_caller_identity" "current" {}

locals {
  name       = var.project
  account_id = data.aws_caller_identity.current.account_id
  image_uri  = "${aws_ecr_repository.this.repository_url}:${var.image_tag}"
}

# ---------------------------------------------------------------------------
# S3: one bucket, two prefixes. batches/ in, results/ out.
#
# Deliberately NOT versioned. Versioning keeps every overwrite forever and is
# the quiet way a demo bucket keeps billing after you think you deleted the
# objects. It also makes terraform destroy fail on a non-empty bucket.
# ---------------------------------------------------------------------------

resource "random_id" "suffix" {
  byte_length = 4
}

resource "aws_s3_bucket" "data" {
  bucket        = "${local.name}-${random_id.suffix.hex}"
  force_destroy = true # so terraform destroy works without manual emptying
}

resource "aws_s3_bucket_public_access_block" "data" {
  bucket                  = aws_s3_bucket.data.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "data" {
  bucket = aws_s3_bucket.data.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "data" {
  bucket = aws_s3_bucket.data.id

  rule {
    id     = "expire-demo-objects"
    status = "Enabled"
    filter {}
    expiration {
      days = var.object_retention_days
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

# ---------------------------------------------------------------------------
# ECR
#
# The lifecycle policy matters. Every `docker push` during development leaves
# the previous image untagged but still stored and still billed. Push twenty
# times over five days and you are paying for twenty ~200 MB images.
# ---------------------------------------------------------------------------

resource "aws_ecr_repository" "this" {
  name                 = local.name
  image_tag_mutability = "MUTABLE"
  force_delete         = true # destroy works even with images present

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "this" {
  repository = aws_ecr_repository.this.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Expire untagged images after 1 day"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = 1
        }
        action = { type = "expire" }
      },
      {
        rulePriority = 2
        description  = "Keep only the 3 most recent tagged images"
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = 3
        }
        action = { type = "expire" }
      }
    ]
  })
}

# ---------------------------------------------------------------------------
# IAM: scoped to this bucket and this log group, nothing wider.
#
# The lazy version of this is AWSLambdaBasicExecutionRole plus
# AmazonS3FullAccess. That grants read and write on every bucket in the
# account, which an interviewer will notice.
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda" {
  name               = "${local.name}-lambda"
  assume_role_policy = data.aws_iam_policy_document.assume.json
}

data "aws_iam_policy_document" "lambda" {
  statement {
    sid       = "ReadBatches"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.data.arn}/batches/*"]
  }

  statement {
    sid       = "WriteResults"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.data.arn}/results/*"]
  }

  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.lambda.arn}:*"]
  }

  # Day 3. Off by default so the role stays minimal until it is actually used.
  dynamic "statement" {
    for_each = var.enable_bedrock ? [1] : []
    content {
      sid     = "InvokeBedrockModel"
      actions = ["bedrock:InvokeModel"]
      resources = [
        "arn:aws:bedrock:${var.region}::foundation-model/${var.bedrock_model_id}"
      ]
    }
  }
}

resource "aws_iam_role_policy" "lambda" {
  name   = "${local.name}-lambda"
  role   = aws_iam_role.lambda.id
  policy = data.aws_iam_policy_document.lambda.json
}

# ---------------------------------------------------------------------------
# CloudWatch Logs
#
# Created explicitly rather than letting Lambda create it implicitly. A log
# group Lambda creates for itself defaults to "Never expire", so a chatty
# function accrues storage charges indefinitely. It also would not be removed
# by terraform destroy, because Terraform never knew about it.
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${local.name}"
  retention_in_days = var.log_retention_days
}

# ---------------------------------------------------------------------------
# Lambda
#
# No VPC config. Putting a Lambda in a VPC to reach the internet requires a
# NAT Gateway at roughly $32/month whether or not it passes a byte, and it is
# the single most common way a portfolio project produces a surprise bill.
# This function only needs S3 and Bedrock, both reachable from outside a VPC.
# ---------------------------------------------------------------------------

resource "aws_lambda_function" "this" {
  function_name = local.name
  role          = aws_iam_role.lambda.arn
  package_type  = "Image"
  image_uri     = local.image_uri
  architectures = [var.architecture]
  memory_size   = var.memory_mb
  timeout       = var.timeout_seconds

  environment {
    variables = {
      OUTPUT_BUCKET = aws_s3_bucket.data.id
      MODEL_PATH    = "/opt/monitor.json"
      TOP_K         = tostring(var.top_k)
      LOG_LEVEL     = var.log_level

      # Only switched on when the IAM role actually grants bedrock:InvokeModel,
      # so the function can never attempt a call it is not permitted to make.
      ENABLE_EXPLANATION = tostring(var.enable_bedrock)
      BEDROCK_MODEL_ID   = var.bedrock_model_id
    }
  }

  depends_on = [
    aws_iam_role_policy.lambda,
    aws_cloudwatch_log_group.lambda,
  ]
}

# Dropping a CSV into batches/ invokes the monitor. This is the whole
# serverless pipeline: no server, no scheduler, no queue to run.
resource "aws_lambda_permission" "s3" {
  count          = var.enable_s3_trigger ? 1 : 0
  statement_id   = "AllowExecutionFromS3"
  action         = "lambda:InvokeFunction"
  function_name  = aws_lambda_function.this.function_name
  principal      = "s3.amazonaws.com"
  source_arn     = aws_s3_bucket.data.arn
  source_account = local.account_id
}

resource "aws_s3_bucket_notification" "batches" {
  count  = var.enable_s3_trigger ? 1 : 0
  bucket = aws_s3_bucket.data.id

  lambda_function {
    lambda_function_arn = aws_lambda_function.this.arn
    events              = ["s3:ObjectCreated:*"]
    filter_prefix       = "batches/"
    filter_suffix       = ".csv"
  }

  depends_on = [aws_lambda_permission.s3]
}
