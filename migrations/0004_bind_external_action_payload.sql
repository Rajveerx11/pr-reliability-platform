ALTER TABLE external_actions
    ADD COLUMN payload_fingerprint varchar(64);

UPDATE external_actions
SET payload_fingerprint = repeat('0', 64)
WHERE payload_fingerprint IS NULL;

ALTER TABLE external_actions
    ALTER COLUMN payload_fingerprint SET NOT NULL,
    ADD CONSTRAINT external_actions_payload_fingerprint_format CHECK (
        payload_fingerprint ~ '^[0-9a-f]{64}$'
    );
