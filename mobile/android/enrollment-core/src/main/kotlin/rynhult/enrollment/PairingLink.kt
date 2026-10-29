package rynhult.enrollment

import java.net.URI

/**
 * The owner's pairing link: `https://<private origin>/pair-mobile#<pairing id>:<token>`.
 *
 * The app already knows which private origin the person chose to pair with, and the link must name exactly that
 * origin: a QR can name an attacker's server even with a valid public certificate (docs/MOBILE-APPS.md). The token
 * is a five-minute, one-use capability and is never logged; `toString` redacts it.
 */
class PairingLink private constructor(val origin: String, val pairingId: String, val token: String) {

    override fun toString() = "PairingLink(origin=$origin, pairingId=$pairingId, token=<redacted>)"

    companion object {
        private val ID = Regex("^[0-9a-f]{32}$")
        private val TOKEN = Regex("^[A-Za-z0-9_-]{43,64}$")

        fun parse(link: String, expectedOrigin: String): PairingLink {
            val uri = try {
                URI(link.trim())
            } catch (error: Exception) {
                throw EnrollmentException("Not a Rynhult pairing link.")
            }
            if (uri.scheme != "https" || uri.rawUserInfo != null || uri.rawQuery != null || uri.host == null) {
                throw EnrollmentException("Not a Rynhult pairing link.")
            }
            if (uri.rawPath != "/pair-mobile") throw EnrollmentException("Not a Rynhult pairing link.")
            val origin = originOf(uri)
            if (origin != canonicalOrigin(expectedOrigin)) {
                throw EnrollmentException("This link is for $origin, not the server you chose. Check the owner's screen.")
            }
            val parts = (uri.rawFragment ?: "").split(":")
            if (parts.size != 2 || !ID.matches(parts[0]) || !TOKEN.matches(parts[1])) {
                throw EnrollmentException("The pairing link is incomplete. Ask the owner for a new one.")
            }
            return PairingLink(origin, parts[0], parts[1])
        }

        /** `https://host[:port]`, lowercase host, default port dropped: the server's configured private origin form. */
        fun canonicalOrigin(origin: String): String {
            val uri = URI(origin.trim())
            if (uri.scheme != "https" || uri.host == null || uri.rawUserInfo != null
                || !(uri.rawPath.isNullOrEmpty() || uri.rawPath == "/") || uri.rawQuery != null || uri.rawFragment != null
            ) throw EnrollmentException("The server address must be a plain https:// origin.")
            return originOf(uri)
        }

        private fun originOf(uri: URI): String {
            val port = if (uri.port == -1 || uri.port == 443) "" else ":${uri.port}"
            return "https://${uri.host.lowercase()}$port"
        }
    }
}
