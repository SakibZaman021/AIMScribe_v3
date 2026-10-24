<?php
/*
 * AIMScribe keeps everything in PostgreSQL, but Adminer's login form opens on
 * "MySQL / MariaDB", and a login sent that way fails with an error that
 * names neither the database nor the cause. The form offers PostgreSQL only.
 *
 * Mounted read-only into plugins-enabled/ (docker-compose.yml, profile db).
 */

class AimsPostgresOnly extends Adminer\Plugin {
	function loginFormField($name, $heading, $value) {
		if ($name == 'driver') {
			return $heading . Adminer\html_select("auth[driver]", array("pgsql" => "PostgreSQL"),
				"pgsql", "loginDriver(this);") . "\n";
		}
	}
}

return new AimsPostgresOnly();
