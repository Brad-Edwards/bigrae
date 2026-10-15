mock_provider "aws" {}
mock_provider "tls" {}

variables {
  name_prefix            = "ngfw-device-12345678-1234-1234-1234-123456789abc"
  instance_uuid          = "12345678-1234-1234-1234-123456789abc"
  request_uuid           = "87654321-1234-1234-1234-123456789abc"
  subnet_id              = "subnet-synthetic"
  mgmt_security_group_id = "sg-synthetic-management"
  data_security_group_id = "sg-synthetic-data"
  ami_id                 = "ami-synthetic"
  bootstrap_bucket       = "synthetic-bootstrap"
  scm_pin_id             = "synthetic-pin"
  scm_pin_value          = "synthetic-registration"
  authcode               = "synthetic-authcode"
  secrets_kms_key_arn    = "arn:aws:kms:us-east-2:123456789012:key/12345678-1234-1234-1234-123456789abc"
}

run "service_device_without_human_owner" {
  command = plan

  assert {
    condition     = startswith(local.hostname, "ngfw-12345678-") && !contains(keys(local.common_tags), "shifter:user_id")
    error_message = "Service devices must use native device identity without a human user tag."
  }

  assert {
    condition     = aws_network_interface.mgmt.description == "NGFW management interface for ${var.name_prefix}"
    error_message = "Device descriptions must remain valid with a null historical user ID."
  }
}

run "human_device_preserves_deployed_identity" {
  command = plan

  variables {
    user_id     = 42
    name_prefix = "ngfw-user-42"
  }

  assert {
    condition     = local.hostname == "ngfw-user-42" && local.common_tags["shifter:user_id"] == "42"
    error_message = "Human-owned devices must retain their deployed identity."
  }
}
