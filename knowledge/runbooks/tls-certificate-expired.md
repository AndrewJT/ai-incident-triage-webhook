# TLS certificate expired

## Symptoms
"x509: certificate has expired or is not yet valid", "SSL handshake failed", `CertificateExpiredError`,
browsers showing NET::ERR_CERT_DATE_INVALID, or service-to-service calls failing with TLS errors.

## Diagnosis
- Inspect the served certificate (`openssl s_client -connect host:443 | openssl x509 -noout -dates`).
- Check whether the automated renewal job (cert-manager / ACME) is failing.

## Remediation
1. Renew or reissue the certificate immediately.
2. Reload or restart the terminating proxy / ingress so it serves the new certificate.
3. Fix the failing renewal job and verify the next scheduled renewal.
4. Add expiry monitoring with alerts at 30, 14 and 7 days.
