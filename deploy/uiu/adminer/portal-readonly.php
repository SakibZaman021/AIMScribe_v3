<?php
/*
 * Signed in to the portal with Google, a person opens the databases without
 * a database password. The gateway has already checked who they are
 * (/auth/check) and passes their role as X-Portal-Role; the gateway sets it
 * on every request to /db/, so a browser cannot claim one.
 *
 * Username "read-only" connects with logins that can only read:
 *   aims_recordings -> aims_monitor          (SELECT on every table and view)
 *   aims_clinical   -> aims_clinical_reader  (SELECT; it can only add to the
 *                                             clinical access log)
 * so a researcher can change or delete nothing, whatever they type - the
 * database itself refuses. An admin may instead sign in with a database
 * account and its own password.
 *
 * Mounted read-only into plugins-enabled/ (docker-compose.yml).
 */

class AimsPortalReadOnly extends Adminer\Plugin {
	const READONLY = "read-only";
	const DATABASES = array("aims_recordings", "aims_clinical");

	private function role() {
		return isset($_SERVER["HTTP_X_PORTAL_ROLE"]) ? $_SERVER["HTTP_X_PORTAL_ROLE"] : "";
	}

	private function readOnly() {
		return $this->role() !== "" && isset($_GET["username"]) && $_GET["username"] === self::READONLY;
	}

	function credentials() {
		if (!$this->readOnly()) {
			return null;
		}
		$db = isset($_GET["db"]) ? $_GET["db"] : "";
		if ($db === "aims_clinical") {
			return array(Adminer\SERVER, "aims_clinical_reader", getenv("AIMS_CLINICAL_READER_PASSWORD"));
		}
		return array(Adminer\SERVER, "aims_monitor", getenv("AIMS_MONITOR_PASSWORD"));
	}

	function login($login, $password) {
		if ($this->role() === "") {
			return "Sign in to the portal first.";
		}
		if ($login === self::READONLY) {
			return true;
		}
		if ($this->role() !== "admin") {
			return "Researchers open the databases read-only: choose the user read-only.";
		}
		return null;   // an admin with a database account: the database decides
	}

	function loginFormField($name, $heading, $value) {
		$role = $this->role();
		if ($role === "") {
			return null;
		}
		if ($name == "username") {
			$lock = ($role === "admin") ? "" : " readonly";
			return $heading . "<input name='auth[username]' value='" . self::READONLY . "'"
				. " autocapitalize='off'" . $lock . ">"
				. ($role === "admin" ? " <small>or a database account</small>" : "") . "\n";
		}
		if ($name == "password") {
			if ($role === "admin") {
				return $heading . "<input type='password' name='auth[password]' autocomplete='current-password'>"
					. " <small>empty for read-only</small>\n";
			}
			return $heading . "<input type='hidden' name='auth[password]' value=''>"
				. "<small>Not needed: you are signed in with Google.</small>\n";
		}
		if ($name == "db") {
			$options = "";
			foreach (self::DATABASES as $db) {
				$options .= "<option" . ($db === $value ? " selected" : "") . ">" . Adminer\h($db) . "</option>";
			}
			return $heading . "<select name='auth[db]'>" . $options . "</select>\n";
		}
		return null;
	}
}

return new AimsPortalReadOnly();
