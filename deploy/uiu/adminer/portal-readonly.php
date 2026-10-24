<?php
/*
 * Signed in to the portal with Google, a person opens the databases with
 * their own read-only login (X-Portal-Db-User, made in the People panel) and
 * its password - and with no other name: the database logs every statement
 * under that name. An admin may also open them without a password. The gateway has already checked who they are
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

	/* This person's own login name, from the gateway (never from the browser). */
	private function own() {
		$name = isset($_SERVER["HTTP_X_PORTAL_DB_USER"]) ? $_SERVER["HTTP_X_PORTAL_DB_USER"] : "";
		return preg_match('/^r_[a-z0-9_]{1,40}$/', $name) ? $name : "";
	}

	private function readOnly() {
		return $this->role() === "admin" && isset($_GET["username"]) && $_GET["username"] === self::READONLY;
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
		if ($this->role() === "admin") {
			// read-only without a password, or any database account
			return $login === self::READONLY ? true : null;
		}
		// A researcher: their own login only, its password checked by the
		// database before this is asked.
		if ($this->own() === "") {
			return "You have no database login yet. Ask an admin to create one in the People panel.";
		}
		if ($login !== $this->own()) {
			return "Use your own database login: " . $this->own() . ".";
		}
		return true;
	}

	function loginFormField($name, $heading, $value) {
		$role = $this->role();
		if ($role === "") {
			return null;
		}
		if ($name == "username") {
			if ($role === "admin") {
				return $heading . "<input name='auth[username]' value='" . self::READONLY . "'"
					. " autocapitalize='off'> <small>or a database account</small>\n";
			}
			return $heading . "<input name='auth[username]' value='" . Adminer\h($this->own()) . "'"
				. " autocapitalize='off' readonly>\n";
		}
		if ($name == "password") {
			return $heading . "<input type='password' name='auth[password]' autocomplete='current-password'>"
				. ($role === "admin" ? " <small>empty for read-only</small>" : " <small>your database password</small>")
				. "\n";
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
