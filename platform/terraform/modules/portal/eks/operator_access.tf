# ------------------------------------------------------------------------------
# Operator access node for the private EKS API endpoint (#2313)
# ------------------------------------------------------------------------------
# The EKS API endpoint is private only. Operators reach it with Session Manager
# port forwarding (AWS-StartPortForwardingSessionToRemoteHost) through this
# node. The node has no public address, no key pair and no inbound rule. Its
# role has only the Session Manager core policy. Operator credentials stay on
# the operator's workstation; the node relays TCP 443 to the endpoint.
# The deploy role may start sessions only on instances with this node's tag.
# ------------------------------------------------------------------------------

data "aws_ssm_parameter" "operator_access_ami" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"
}

resource "aws_iam_role" "operator_access" {
  name                 = "${var.cluster_name}-operator-access"
  permissions_boundary = var.permissions_boundary_arn

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = var.tags
}

resource "aws_iam_role_policy_attachment" "operator_access_ssm" {
  role       = aws_iam_role.operator_access.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "operator_access" {
  name = "${var.cluster_name}-operator-access"
  role = aws_iam_role.operator_access.name
  tags = var.tags
}

resource "aws_security_group" "operator_access" {
  name        = "${var.cluster_name}-operator-access"
  description = "Operator access node: no inbound; HTTPS to the EKS API and Session Manager"
  vpc_id      = aws_vpc.this.id

  egress {
    description     = "Kubernetes API through the private endpoint"
    from_port       = 443
    to_port         = 443
    protocol        = "tcp"
    security_groups = [aws_eks_cluster.this.vpc_config[0].cluster_security_group_id]
  }

  egress {
    description = "Session Manager and AWS APIs through NAT"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(var.tags, {
    Name = "${var.cluster_name}-operator-access"
  })
}

resource "aws_security_group_rule" "cluster_api_from_operator_access" {
  type                     = "ingress"
  from_port                = 443
  to_port                  = 443
  protocol                 = "tcp"
  security_group_id        = aws_eks_cluster.this.vpc_config[0].cluster_security_group_id
  source_security_group_id = aws_security_group.operator_access.id
  description              = "Kubernetes API from the operator access node"
}

resource "aws_instance" "operator_access" {
  ami                         = data.aws_ssm_parameter.operator_access_ami.value
  instance_type               = "t4g.nano"
  subnet_id                   = values(aws_subnet.private)[0].id
  vpc_security_group_ids      = [aws_security_group.operator_access.id]
  iam_instance_profile        = aws_iam_instance_profile.operator_access.name
  associate_public_ip_address = false
  monitoring                  = true
  ebs_optimized               = true

  metadata_options {
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
    http_endpoint               = "enabled"
  }

  root_block_device {
    encrypted   = true
    volume_type = "gp3"
    volume_size = 8
  }

  lifecycle {
    # A new AMI release must not replace the node outside a planned change.
    ignore_changes = [ami]
  }

  tags = merge(var.tags, {
    Name                      = "${var.cluster_name}-operator-access"
    "shifter:operator-access" = var.environment
  })
}
