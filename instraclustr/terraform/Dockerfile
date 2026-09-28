# The root ceremony's toolbox (ceremony.sh): Terraform and the Vault CLI in
# one container, so the root token generated from the unseal shares exists
# only in this container's memory -- never in an environment variable another
# container can inspect, never on disk.
#
# Providers are baked in at build time and verified against the committed
# .terraform.lock.hcl, so the ceremony itself needs no network access.
FROM hashicorp/vault:1.15.2@sha256:6b4e5dadf082cb3587930ced09b52851c5ae9f8d1c7df31ed39f0085a3029f63 AS vault

FROM hashicorp/terraform:1.16.4@sha256:985cdc6c1d9b0a65b83377f666efd2f740b47f02ac55be1ced3d18f7d3b0e829
COPY --from=vault /bin/vault /usr/local/bin/vault
COPY main.tf .terraform.lock.hcl /opt/terraform/src/
RUN terraform -chdir=/opt/terraform/src providers mirror /opt/terraform/providers \
    && rm -rf /opt/terraform/src \
    && printf '%s\n' \
        'provider_installation {' \
        '  filesystem_mirror {' \
        '    path    = "/opt/terraform/providers"' \
        '    include = ["registry.terraform.io/*/*"]' \
        '  }' \
        '  direct {' \
        '    exclude = ["registry.terraform.io/*/*"]' \
        '  }' \
        '}' > /opt/terraform/cli.tfrc
ENV TF_CLI_CONFIG_FILE=/opt/terraform/cli.tfrc \
    TF_IN_AUTOMATION=1
ENTRYPOINT ["/bin/sh", "/terraform/ceremony.sh"]
