# gcp-deploy initializes Identity Platform (initializeAuth) before Terraform
# runs. The provider can not create a config that is already enabled, so the
# root adopts the existing singleton. A CLI `terraform import` fails on a fresh
# state, because it can not resolve the for_each keys of resources that do not
# exist yet; an import block is part of the normal plan (#2598).
import {
  to = module.platform_core.module.portal_identity_platform.google_identity_platform_config.platform
  id = var.project_id
}
