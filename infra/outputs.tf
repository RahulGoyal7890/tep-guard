output "bucket" {
  description = "S3 bucket for batches/ and results/"
  value       = aws_s3_bucket.data.id
}

output "ecr_repository_url" {
  description = "Push the container image here"
  value       = aws_ecr_repository.this.repository_url
}

output "function_name" {
  value = aws_lambda_function.this.function_name
}

output "log_group" {
  value = aws_cloudwatch_log_group.lambda.name
}

output "region" {
  value = var.region
}

output "smoke_test" {
  description = "Copy-paste command to prove the deployed function works"
  value       = "aws lambda invoke --function-name ${aws_lambda_function.this.function_name} --cli-binary-format raw-in-base64-out --payload file://payload.json --region ${var.region} out.json && cat out.json"
}
