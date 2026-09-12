variable "project" {
  description = "Name prefix for every resource. Keep it short and lowercase."
  type        = string
  default     = "tep-guard"
}

variable "region" {
  description = "AWS region. Bedrock model availability varies, so us-east-1 is the safe default."
  type        = string
  default     = "us-east-1"
}

variable "image_tag" {
  description = "ECR image tag the Lambda points at."
  type        = string
  default     = "latest"
}

variable "architecture" {
  description = <<-EOT
    x86_64 or arm64. arm64 (Graviton) is around 20% cheaper per GB-second, but
    cross-building an arm64 image from an x86 laptop needs docker buildx with
    qemu and is slow. x86_64 by default because a working deploy beats a
    marginally cheaper one that will not build.
  EOT
  type        = string
  default     = "x86_64"

  validation {
    condition     = contains(["x86_64", "arm64"], var.architecture)
    error_message = "architecture must be x86_64 or arm64."
  }
}

variable "memory_mb" {
  description = <<-EOT
    Lambda memory. CPU scales with it, so more memory can be cheaper overall
    when it cuts duration. 512 MB comfortably fits a 24-component PCA over a
    960-sample batch, which runs in single-digit milliseconds.
  EOT
  type        = number
  default     = 512
}

variable "timeout_seconds" {
  description = "Well above the observed few-millisecond runtime, but low enough that a hang cannot bill for 15 minutes."
  type        = number
  default     = 30
}

variable "log_retention_days" {
  description = "CloudWatch retention. AWS defaults to never expire; do not."
  type        = number
  default     = 7
}

variable "object_retention_days" {
  description = "S3 lifecycle expiry for demo batches and results."
  type        = number
  default     = 7
}

variable "top_k" {
  description = "How many suspect variables the contribution analysis reports."
  type        = number
  default     = 3
}

variable "log_level" {
  type    = string
  default = "INFO"
}

variable "enable_s3_trigger" {
  description = "Invoke the function when a CSV lands in batches/."
  type        = bool
  default     = true
}

variable "enable_bedrock" {
  description = "Adds bedrock:InvokeModel for one model to the Lambda role, and switches on operator explanations."
  type        = bool
  default     = true
}

variable "bedrock_model_id" {
  description = "Exact model id the role may invoke. Scoped to one model, not a wildcard."
  type        = string
  default     = "amazon.nova-micro-v1:0"
}
