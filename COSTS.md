# Costs

Target for this project: **under $2 total**, and $0 once destroyed.

Every resource here is either free-tier covered or costs cents at demo scale.
That is not an accident — each of the traps below is a real way portfolio
projects generate bills, and each is defused by a specific line of Terraform.

## What this actually costs

| Service | Usage at demo scale | Cost |
| --- | --- | --- |
| Lambda | a few hundred invocations, ~5 ms at 512 MB | effectively $0 |
| S3 | a few MB, expired after 7 days | under $0.01 |
| ECR | up to 3 images, ~250 MB each | ~$0.02/month |
| CloudWatch Logs | a few MB, 7-day retention | under $0.01 |
| Bedrock (day 3) | a few hundred Nova Micro calls | a few cents |

Lambda's free tier covers 1M requests and 400,000 GB-seconds per month. This
function uses a rounding error of that.

## Traps, and where each is handled

### NAT Gateway — about $32/month

The big one. A Lambda placed in a VPC loses default internet access, and the
standard fix is a NAT Gateway. It bills roughly $0.045/hour plus data
processing **whether or not a single byte flows through it**, and it is not
free-tier eligible. Leave one running for a month and it costs more than
everything else in this table combined by a factor of a thousand.

*Handled:* `aws_lambda_function.this` has no `vpc_config`. This function talks
to S3 and Bedrock, both reachable from outside a VPC. A VPC would add cost and
cold-start latency and buy nothing.

### ECR image accumulation

Each `docker push` leaves the previous image untagged but still stored and
still billed at $0.10/GB-month. A ~250 MB image pushed twenty times across a
week of iteration is 5 GB of images you forgot exist.

*Handled:* `aws_ecr_lifecycle_policy.this` expires untagged images after 1 day
and keeps only the 3 most recent tagged ones.

### CloudWatch Logs default retention

If Lambda creates its own log group, retention defaults to **Never expire**.
Logs accrue at $0.50/GB-month forever, and because Terraform never created the
group, `terraform destroy` leaves it behind.

*Handled:* `aws_cloudwatch_log_group.lambda` is declared explicitly with
`retention_in_days = 7`, so Terraform owns it and destroys it.

### S3 versioning

Versioning keeps every overwrite indefinitely. Delete the objects and you are
still billed for the versions. It also makes `terraform destroy` fail, because
a versioned bucket is not empty even when it looks empty.

*Handled:* no versioning resource, plus `force_destroy = true` and a lifecycle
rule expiring objects after 7 days.

### DynamoDB on-demand

Not used here, but worth knowing: on-demand mode has no free tier, while
provisioned mode gives 25 read and 25 write capacity units free. The console
defaults to on-demand.

### Provisioned concurrency

Removes Lambda cold starts, and bills continuously for reserved capacity
whether or not anything is invoked. Never appropriate for a demo.

*Handled:* not configured. Cold starts are ~1-2 s for a container image, which
is irrelevant for batch monitoring.

### SageMaker endpoints

Not used here, and the reason this project is Lambda-based. A SageMaker
real-time endpoint bills **per hour while idle**. `ml.t2.medium` is about
$0.05/hour, roughly $36/month for a model answering nothing. For batch
inference on a 30 KB model, Lambda is both cheaper and simpler.

## Guardrails

Set before deploying anything:

1. **AWS Budgets alert at $1.** Billing → Budgets → Zero spend budget. Do this
   first, always.
2. **Everything is tagged** `Project=tep-guard, Ephemeral=true` via the
   provider's `default_tags`, so Cost Explorer can attribute spend and any
   survivor is easy to find.
3. **Destroy after the demo.** `./deploy.sh destroy` removes everything and
   then queries AWS to confirm nothing remains.

## Verifying zero spend after teardown

```bash
./deploy.sh destroy
```

That runs `terraform destroy` and then checks for surviving Lambda functions,
ECR repositories and S3 buckets. Empty output means clean.

Check Cost Explorer 24 hours later, filtered to `Project=tep-guard`. Billing
data lags by up to a day, so an immediate check proves nothing.
