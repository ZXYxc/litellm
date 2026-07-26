CREATE TABLE "LiteLLM_ChatGPTOAuthAccountTable" (
    "credential_id" TEXT NOT NULL,
    "credential_name" TEXT NOT NULL,
    "provider" TEXT NOT NULL DEFAULT 'chatgpt',
    "email_encrypted" TEXT NOT NULL,
    "email_fingerprint" TEXT NOT NULL,
    "account_id_encrypted" TEXT NOT NULL,
    "token_record_encrypted" TEXT NOT NULL,
    "status" TEXT NOT NULL DEFAULT 'active',
    "expires_at" TIMESTAMP(3) NOT NULL,
    "token_version" INTEGER NOT NULL DEFAULT 1,
    "refresh_lease_owner" TEXT,
    "refresh_lease_until" TIMESTAMP(3),
    "refresh_failure_count" INTEGER NOT NULL DEFAULT 0,
    "refresh_backoff_until" TIMESTAMP(3),
    "last_refresh_error" TEXT,
    "created_at" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "created_by" TEXT NOT NULL,
    "updated_at" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_by" TEXT NOT NULL,

    CONSTRAINT "LiteLLM_ChatGPTOAuthAccountTable_pkey" PRIMARY KEY ("credential_id")
);

CREATE TABLE "LiteLLM_ChatGPTOAuthFlowTable" (
    "flow_id" TEXT NOT NULL,
    "credential_id" TEXT,
    "encrypted_flow_record" TEXT NOT NULL,
    "requested_by" TEXT NOT NULL,
    "purpose" TEXT NOT NULL,
    "status" TEXT NOT NULL DEFAULT 'pending',
    "expires_at" TIMESTAMP(3) NOT NULL,
    "created_at" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "LiteLLM_ChatGPTOAuthFlowTable_pkey" PRIMARY KEY ("flow_id")
);

CREATE UNIQUE INDEX "LiteLLM_ChatGPTOAuthAccountTable_credential_name_key" ON "LiteLLM_ChatGPTOAuthAccountTable"("credential_name");
CREATE UNIQUE INDEX "LiteLLM_ChatGPTOAuthAccountTable_email_fingerprint_key" ON "LiteLLM_ChatGPTOAuthAccountTable"("email_fingerprint");
CREATE INDEX "LiteLLM_ChatGPTOAuthAccountTable_status_expires_at_idx" ON "LiteLLM_ChatGPTOAuthAccountTable"("status", "expires_at");
CREATE INDEX "LiteLLM_ChatGPTOAuthAccountTable_refresh_lease_until_idx" ON "LiteLLM_ChatGPTOAuthAccountTable"("refresh_lease_until");
CREATE INDEX "LiteLLM_ChatGPTOAuthFlowTable_requested_by_status_idx" ON "LiteLLM_ChatGPTOAuthFlowTable"("requested_by", "status");
CREATE INDEX "LiteLLM_ChatGPTOAuthFlowTable_expires_at_idx" ON "LiteLLM_ChatGPTOAuthFlowTable"("expires_at");
