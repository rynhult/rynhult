package rynhult.enrollment

/**
 * A person's sign-in: the session cookie and its CSRF token. Held in memory only (the design keeps session secrets
 * behind platform authentication, and v1 has no locked refresh); `toString` redacts both.
 *
 * Over HTTPS the server uses `__Host-` cookies; its test fixtures run without TLS and use the plain names.
 */
class Session(val token: String, val csrf: String, val secure: Boolean = true) {
    fun headers(origin: String): Map<String, String> = mapOf(
        "Cookie" to "${SessionCookies.name("acc_session", secure)}=$token",
        "X-ACC-CSRF" to csrf,
        "Origin" to origin,
        "Content-Type" to "application/json",
    )

    override fun toString() = "Session(token=<redacted>, csrf=<redacted>, secure=$secure)"
}

/** Reads the rotated cookies from a bind answer, refusing any shape the server's HTTPS login cookies don't have. */
object SessionCookies {
    fun name(base: String, secure: Boolean) = if (secure) "__Host-$base" else base

    fun parse(setCookies: List<String>, secure: Boolean): Session {
        val cookies = setCookies.map(::cookie).associateBy { it.name }
        val session = cookies[name("acc_session", secure)] ?: refuse()
        val csrf = cookies[name("acc_csrf", secure)] ?: refuse()
        for (cookie in listOf(session, csrf)) {
            if (cookie.value.isEmpty() || cookie.attributes["path"] != "/" || "domain" in cookie.attributes) refuse()
            if ((cookie.attributes["max-age"]?.toLongOrNull() ?: 0) <= 0) refuse()
            if (secure && "secure" !in cookie.attributes) refuse()
        }
        if ("httponly" !in session.attributes) refuse()
        return Session(session.value, csrf.value, secure)
    }

    private class Cookie(val name: String, val value: String, val attributes: Map<String, String>)

    private fun cookie(header: String): Cookie {
        val parts = header.split(";").map { it.trim() }
        val pair = parts.first().split("=", limit = 2)
        if (pair.size != 2) refuse()
        val attributes = parts.drop(1).filter { it.isNotEmpty() }.associate {
            val attribute = it.split("=", limit = 2)
            attribute[0].lowercase() to (attribute.getOrNull(1) ?: "")
        }
        return Cookie(pair[0], pair[1], attributes)
    }

    private fun refuse(): Nothing = throw EnrollmentException("The server's sign-in answer was incomplete; sign in again.")
}
