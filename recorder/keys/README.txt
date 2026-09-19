Public keys shipped with the installer.

aimslab_grant_pub.pem    verifies the recording grant the AIMS LAB server
                         signs (SRS 3.2 §5). Without it no recording can
                         be authorised.
aimslab_receipt_pub.pem  verifies purge receipts from the AIMS LAB server.
                         Without it local audio is never deleted - safe,
                         but the disk fills.

These are public halves. They are identical on every doctor PC and carry
no secret. The private halves stay on the AIMS LAB server and must never
appear here. CMED holds no key at all.
