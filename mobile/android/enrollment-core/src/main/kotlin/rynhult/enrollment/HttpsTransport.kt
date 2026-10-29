package rynhult.enrollment

import java.io.ByteArrayOutputStream
import java.io.InputStream
import java.net.URI
import javax.net.ssl.HttpsURLConnection
import javax.net.ssl.SSLSocketFactory

/**
 * HTTPS to exactly one origin. Redirects are never followed (credentials must never follow them), caches are off,
 * and answers are size-capped. [sockets] decides trust and identity: on the LAN, server trust pinned to the
 * installation's CA and no client certificate; on the public side, the same pinning plus the Keystore identity
 * (enrollment-android's KeystoreKeyManager). Never a trust-all manager or a global trust store change.
 */
class HttpsTransport(origin: String, private val sockets: SSLSocketFactory, private val timeoutMillis: Int = 15_000) : Transport {
    private val origin = PairingLink.canonicalOrigin(origin)

    override fun send(method: String, path: String, jsonBody: String?, headers: Map<String, String>): HttpResponse {
        require(path.startsWith("/") && !path.startsWith("//")) { "a path on the configured origin" }
        val url = URI(origin + path).toURL()
        check(url.protocol == "https" && "${url.protocol}://${url.authority}".let(PairingLink::canonicalOrigin) == origin)
        val connection = url.openConnection() as HttpsURLConnection
        try {
            connection.sslSocketFactory = sockets
            connection.instanceFollowRedirects = false
            connection.useCaches = false
            connection.connectTimeout = timeoutMillis
            connection.readTimeout = timeoutMillis
            connection.requestMethod = method
            for ((name, value) in headers) connection.setRequestProperty(name, value)
            if (jsonBody != null) {
                connection.doOutput = true
                connection.outputStream.use { it.write(jsonBody.toByteArray(Charsets.UTF_8)) }
            }
            val status = connection.responseCode
            val stream: InputStream? = if (status < 400) connection.inputStream else connection.errorStream
            val body = stream?.use { readCapped(it) } ?: ""
            val cookies = connection.headerFields.entries
                .filter { it.key?.equals("Set-Cookie", ignoreCase = true) == true }  // the status line's key is null
                .flatMap { it.value }
            return HttpResponse(status, body, cookies)
        } finally {
            connection.disconnect()
        }
    }

    private fun readCapped(stream: InputStream): String {  // InputStream.readNBytes needs Android 13
        val out = ByteArrayOutputStream()
        val buffer = ByteArray(8192)
        while (true) {
            val read = stream.read(buffer)
            if (read < 0) break
            out.write(buffer, 0, read)
            if (out.size() > MAX_BODY) throw EnrollmentException("The server's answer was too large.")
        }
        return out.toString(Charsets.UTF_8.name())
    }

    private companion object {
        const val MAX_BODY = 64 * 1024
    }
}
