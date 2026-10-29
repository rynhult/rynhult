package rynhult.enrollment

import kotlinx.serialization.Serializable
import java.io.File
import java.io.FileOutputStream
import java.nio.file.Files
import java.nio.file.StandardCopyOption
import java.security.SecureRandom

/**
 * What the app remembers about its enrollment with one server. Nothing in it is secret: the key stays in the
 * Keystore, and the pairing token, receipt and session cookie are only ever held in memory.
 *
 * The installation ID is random per installation and changes whenever the identity is forgotten or revoked: the
 * server refuses a second enrollment of the same installation, and a fresh installation must not reuse one.
 */
@Serializable
data class EnrollmentState(
    val origin: String,
    val installationId: String,
    val alias: String? = null,       // the Keystore alias of the current identity
    val deviceId: String? = null,
    val caSha256: String? = null,    // the approved device CA every issued chain matched
    val fingerprint: String? = null, // SHA-256 of the leaf, as the server's device listing shows it
    val expiresAt: String? = null,   // the leaf's notAfter, ISO 8601
    val bound: Boolean = false,      // a session is bound to this identity (cleared by a claim, rotation or 401)
    val pendingAlias: String? = null, // the staged key of an unfinished claim; while set nothing uses the identity
) {
    val enrolled get() = alias != null && deviceId != null

    companion object {
        fun fresh(origin: String) = EnrollmentState(PairingLink.canonicalOrigin(origin), newInstallationId())

        fun newInstallationId(): String {
            val bytes = ByteArray(16).also { SecureRandom().nextBytes(it) }
            return bytes.joinToString("") { "%02x".format(it) }
        }
    }
}

interface StateStore {
    fun load(): EnrollmentState?
    fun save(state: EnrollmentState)
    fun clear()
}

/**
 * A JSON file, replaced atomically. On Android put it in `Context.noBackupFilesDir`, so backups and device transfers
 * never carry it to another phone (identities are never synced: docs/MOBILE-APPS.md).
 */
class FileStateStore(private val file: File) : StateStore {
    /**
     * The saved record, or null when there is none or it can't be read (torn by a crash, say). Unreadable counts as
     * not enrolled: the client starts over, and its recover() and forget() delete the keys the record named.
     */
    override fun load(): EnrollmentState? = try {
        if (file.exists()) Api.decode(EnrollmentState.serializer(), file.readText()) else null
    } catch (error: EnrollmentException) {
        null
    }

    override fun save(state: EnrollmentState) {
        val temporary = File(file.parentFile, file.name + ".tmp")
        // Synced before the rename: after a power loss the record is the old one or the new one, never empty.
        FileOutputStream(temporary).use { stream ->
            stream.write(Api.json.encodeToString(EnrollmentState.serializer(), state).toByteArray(Charsets.UTF_8))
            stream.fd.sync()
        }
        Files.move(temporary.toPath(), file.toPath(), StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING)
    }

    override fun clear() {
        file.delete()
    }
}

class MemoryStateStore(private var state: EnrollmentState? = null) : StateStore {
    override fun load() = state
    override fun save(state: EnrollmentState) { this.state = state }
    override fun clear() { state = null }
}
