terraform {
  # Bucket and prefix come from -backend-config="bucket=<project>-terraform-state"
  # and -backend-config="prefix=release-registry" at init time. The release
  # registry outlives every deployment, so it keeps its own state prefix.
  backend "gcs" {
    bucket = "OVERRIDDEN_VIA_BACKEND_CONFIG"
    prefix = "OVERRIDDEN_VIA_BACKEND_CONFIG"
  }
}
