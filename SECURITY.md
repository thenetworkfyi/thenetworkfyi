# Security Policy

## Security Model and THE SEAL

The core security invariant of The Network is **THE SEAL**: prompt injection must not be able to exfiltrate user identities or data, even under a fully hijacked model. Leakage is made structurally impossible through architectural boundaries and search chokepoints rather than prompt compliance alone. For the detailed security architecture and red-team invariants, refer to [docs/security.md](docs/security.md).

## Reporting a Vulnerability

If you discover a security vulnerability or potential SEAL bypass in this repository, please report it privately. Do not open a public issue or discussion.

Submit reports via **GitHub Private Vulnerability Reporting** on the repository page under **Security > Report a vulnerability**.

### Response Expectations

- **Acknowledgement:** We aim to acknowledge receipt of private vulnerability reports within 48 hours.
- **Assessment & Status:** We will assess the report and provide status updates as triage progresses.
- **Disclosure & Patching:** Confirmed vulnerabilities will be remediated in a security release before public advisory publication.
