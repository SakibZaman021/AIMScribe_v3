Public keys shipped with the installer.

cmed_grant_pub.pem       verifies the recording grant CMED signs.
                         Without it the agent refuses to record.
aimslab_receipt_pub.pem  verifies purge receipts from the AIMS LAB server.
                         Without it local audio is never deleted - safe,
                         but the disk fills.

These are public halves. They are identical on every doctor PC and carry
no secret. The private halves stay on the CMED deployment and the backend
and must never appear here.
