package rynhult.enrollment

import java.security.SecureRandom
import java.time.Duration
import java.time.Instant
import java.time.OffsetDateTime

/** A claim waiting for the owner. The receipt is a capability: memory only, redacted from logs. */
class PendingClaim internal constructor(val alias: String, internal val receipt: String, val matchingCode: String) {
    override fun toString() = "PendingClaim(alias=$alias, matchingCode=$matchingCode, receipt=<redacted>)"
}

sealed interface CredentialOutcome {
    /** The owner hasn't typed the code yet (or the credential isn't current). Ask again in a few seconds. */
    data object Waiting : CredentialOutcome
    /** The five-minute pairing ended or was cancelled. Its key is deleted; start over with a new link. */
    data object Expired : CredentialOutcome
    data object Throttled : CredentialOutcome
    /** Stored and switched to. Sign in and bind next: `pending`/`awaiting_connection` until the edge is verified. */
    data class Enrolled(val deviceId: String, val activation: Activation, val rotated: Boolean) : CredentialOutcome
}

sealed interface DeviceStatus {
    data class Active(val activation: Activation, val renewWithinDays: Int?) : DeviceStatus
    /** Revoked, or no longer this phone's identity, per the server: the identity was forgotten. */
    data object Revoked : DeviceStatus
    data object Expired : DeviceStatus
    /** The session ended (revoking another device of the person does that): sign in and bind again. */
    data object SignedOut : DeviceStatus
    /** No bound session: after pairing, a rotation or a sign-out the person signs in and binds first. */
    data object NotBound : DeviceStatus
    data object NotEnrolled : DeviceStatus
}

/**
 * Enrollment, rotation, session binding and revocation for one server, following the client lifecycle rules in
 * docs/MOBILE-APPS.md (the stricter behaviour of the iOS and Android clients, security review 2026-10-25).
 *
 * [person] and [issuerSha256] (the device-issuer CA's SHA-256) are approved up front, by comparing them with the
 * owner's trusted screen; the CA is never learned from the first credential. [lan] reaches the private origin with
 * ordinary, pinned server TLS and no client identity; enrollment only happens there. The public side passes its own
 * transport, which presents the Keystore identity, to [bindSession] and [status]. [onIdentityChanged] must drop
 * pooled TLS connections and the host's session: after a claim, rotation or revocation nothing may keep using them.
 * Call [recover] when the app starts and when it locks: an unfinished claim's receipt lived only in memory.
 */
class EnrollmentClient(
    origin: String,
    private val person: String,
    issuerSha256: String,
    private val lan: Transport,
    private val keys: DeviceKeys,
    private val store: StateStore,
    private val clock: () -> Instant = Instant::now,
    private val onIdentityChanged: () -> Unit = {},
) {
    private val origin = PairingLink.canonicalOrigin(origin)
    private val issuerSha256 = issuerSha256.also {
        if (!Regex("^[0-9a-f]{64}$").matches(it) || person.isBlank()) throw EnrollmentException("Approve the server's person and device CA first.")
    }

    val state: EnrollmentState
        get() = store.load()?.also {
            if (it.origin != origin) throw EnrollmentException("This phone is enrolled with ${it.origin}; forget it first.")
        } ?: EnrollmentState.fresh(origin).also(store::save)

    /** Scan or paste the owner's link: make a fresh key, claim with it and return the code to show the owner. */
    fun claim(linkText: String): PendingClaim {
        val current = state
        if (current.pendingAlias != null) throw EnrollmentException("A pairing is already in progress. Finish it, or start over.")
        val link = PairingLink.parse(linkText, current.origin)
        val alias = ALIAS_PREFIX + hex(8)
        // A claim ends the session first. A rotation keeps the working identity, unused while claiming, until the
        // new leaf is installed; the staged alias is saved so an interrupted claim can always be cleaned up.
        store.save(current.copy(pendingAlias = alias, bound = false))
        if (current.enrolled) onIdentityChanged()
        return try {
            val publicKey = keys.create(alias)
            val csr = Csr.pem(publicKey) { keys.sign(alias, it) }
            val proof = ClaimProof.encode(keys.sign(alias, ClaimProof.message(link, current.installationId, csr)))
            val body = ClaimRequest(link.token, current.installationId, Api.PLATFORM, csr, proof)
            val response = lan.send("POST", Api.PREFIX + "/claim", Api.json.encodeToString(ClaimRequest.serializer(), body), JSON)
            if (response.status != 200) throw refused(response.status)
            val claimed = Api.decode(ClaimResponse.serializer(), response.body)
            if (!Regex("^[0-9]{6}$").matches(claimed.matchingCode)) throw EnrollmentException("Unexpected claim answer.")
            PendingClaim(alias, claimed.receipt, claimed.matchingCode)
        } catch (error: Exception) {
            dropPending()  // a refused or broken claim never leaves a key behind; a rotation keeps its old identity
            throw error
        }
    }

    /** Ask whether the owner has confirmed; on success validate, store and switch to the new identity atomically. */
    fun credential(pending: PendingClaim): CredentialOutcome {
        val response = lan.send("POST", Api.PREFIX + "/credential",
            Api.json.encodeToString(ReceiptRequest.serializer(), ReceiptRequest(pending.receipt)), JSON)
        when (response.status) {
            200 -> {}
            409 -> return CredentialOutcome.Waiting
            410 -> { dropPending(); return CredentialOutcome.Expired }
            429 -> return CredentialOutcome.Throttled
            else -> throw refused(response.status)
        }
        val current = state
        if (current.pendingAlias != pending.alias) throw EnrollmentException("That pairing was abandoned; start over.")
        val publicKey = keys.publicKey(pending.alias) ?: throw EnrollmentException("The new key is gone; pair again.")
        val issued = Api.decode(CredentialResponse.serializer(), response.body)
        val identity = try {
            CredentialValidator.validate(issued, publicKey, clock(), issuerSha256, current.deviceId)
        } catch (error: EnrollmentException) {
            // On a rotation the owner already approved the replacement, so the old leaf is superseded too.
            if (current.enrolled) forget() else dropPending()
            throw error
        }
        keys.storeChain(pending.alias, listOf(identity.leaf, identity.ca))
        // The commit point: once saved, the new alias is the identity, unbound. Zero overlap, as the server's renewal.
        store.save(current.copy(alias = pending.alias, pendingAlias = null, deviceId = issued.deviceId,
                                caSha256 = identity.caSha256, fingerprint = CredentialValidator.sha256Hex(identity.leaf.encoded),
                                expiresAt = identity.leaf.notAfter.toInstant().toString(), bound = false))
        current.alias?.takeIf { it != pending.alias }?.let(keys::delete)
        onIdentityChanged()
        return CredentialOutcome.Enrolled(issued.deviceId, Activation.of(issued.activation), rotated = current.alias != null)
    }

    /**
     * After a normal sign-in through the public edge with this identity: check it is the approved person, then bind
     * the session to the identity. A 403 only ends the session (it can be a recent-verification refusal); a 404 or 410
     * forgets the identity. Call [status] next.
     */
    fun bindSession(edge: Transport, publicOrigin: String, session: Session): Session {
        val current = state
        if (!current.enrolled || current.pendingAlias != null) throw EnrollmentException("Pair this phone first, or finish the pairing.")
        store.save(current.copy(bound = false))
        val signIn = edge.send("GET", "/api/auth/state", null, session.headers(publicOrigin))
        refusedWhileBinding(signIn.status)
        val who = Api.decode(SignInState.serializer(), signIn.body)
        if (!who.authenticated || who.username != person || who.stepUpRequired == true) {
            throw EnrollmentException("This sign-in isn't $person's, or it still needs verification.", 403)
        }
        val response = edge.send("POST", Api.PREFIX + "/session/bind", "{}", session.headers(publicOrigin))
        if (response.status == 409) throw EnrollmentException("The server hasn't finished activating this phone yet. Try again shortly.", 409)
        refusedWhileBinding(response.status)
        val bound = SessionCookies.parse(response.setCookies, secure = publicOrigin.startsWith("https://"))
        store.save(state.copy(bound = true))
        return bound
    }

    private fun refusedWhileBinding(status: Int) {
        when (status) {
            200 -> return
            401 -> throw EnrollmentException("Sign in again first.", 401)
            403 -> throw EnrollmentException("Sign in again, and verify if asked.", 403)
            404, 410 -> { forget(); throw EnrollmentException("This phone's identity is no longer valid; pair again.", status) }
            else -> throw refused(status)
        }
    }

    /**
     * Recheck this device with the server (on unlock and foreground). The identity is forgotten on local expiry, a
     * 403/404/410, or a listing that doesn't show exactly this device: same person, native, active, the same leaf
     * fingerprint and expiry. A 401 only ends the binding.
     */
    fun status(transport: Transport, requestOrigin: String, session: Session): DeviceStatus {
        val current = state
        val deviceId = current.deviceId ?: return DeviceStatus.NotEnrolled
        expiredAt(current)?.let { forget(); return DeviceStatus.Expired }
        if (!current.bound || current.pendingAlias != null) return DeviceStatus.NotBound
        val response = transport.send("GET", Api.PREFIX + "/devices", null, session.headers(requestOrigin))
        when (response.status) {
            200 -> {}
            401 -> { store.save(current.copy(bound = false)); return DeviceStatus.SignedOut }
            403, 404, 410 -> { forget(); return DeviceStatus.Revoked }
            else -> throw refused(response.status)
        }
        val device = Api.decode(DeviceList.serializer(), response.body).devices.firstOrNull { it.id == deviceId }
        val activation = device?.let { Activation.of(it.activation) }
        if (device == null || activation == null || device.person != person || device.kind != "native" || device.status != "active"
            || activation == Activation.INACTIVE || device.fingerprint != current.fingerprint || !sameInstant(device.expiresAt, current.expiresAt)
        ) {
            forget()
            return DeviceStatus.Revoked
        }
        return DeviceStatus.Active(activation, renewWithinDays(current))
    }

    /**
     * Forget this server's identity: delete the keys and chain and start a new installation ID, so a later pairing is
     * a fresh enrollment (the server keeps the old installation's record). Local sessions are the caller's to drop.
     */
    fun forget() {
        store.load()?.let { old -> listOfNotNull(old.alias, old.pendingAlias).forEach(keys::delete) }
        keys.aliases(ALIAS_PREFIX).forEach(keys::delete)  // also any key an unreadable or lost record left behind
        store.save(EnrollmentState.fresh(origin))
        onIdentityChanged()
    }

    /**
     * At app start and on lock: an unfinished claim can't complete (its receipt was in memory), so drop its key; and
     * delete any identity key the record no longer names (a crash between the rotation commit and deleting the old
     * key, or a record that was unreadable and started over).
     */
    fun recover() {
        if (state.pendingAlias != null) dropPending()
        val current = state.alias
        keys.aliases(ALIAS_PREFIX).filter { it != current }.forEach(keys::delete)
    }

    /** 14, 7 or 1 when renewal is that close (the design's warnings), else null. */
    fun renewWithinDays(current: EnrollmentState = state): Int? {
        val expires = current.expiresAt?.let(Instant::parse) ?: return null
        val left = Duration.between(clock(), expires)
        return listOf(1L, 7L, 14L).firstOrNull { left <= Duration.ofDays(it) }?.toInt()
    }

    /**
     * Abandon an unfinished claim. A first enrollment also starts a new installation ID: the owner may already have
     * confirmed it, and the server's device record then holds this installation, refusing it ever after ("already
     * enrolled"). A rotation keeps its installation ID, which the server requires for it.
     */
    private fun dropPending() {
        val current = store.load() ?: return
        current.pendingAlias?.let(keys::delete)
        val installation = if (current.enrolled) current.installationId else EnrollmentState.newInstallationId()
        store.save(current.copy(pendingAlias = null, installationId = installation))
    }

    private fun sameInstant(listed: String?, stored: String?): Boolean {
        if (listed == null || stored == null) return false
        val difference = OffsetDateTime.parse(listed).toEpochSecond() - Instant.parse(stored).epochSecond
        return difference in -1..1
    }

    private fun expiredAt(current: EnrollmentState): Instant? =
        current.expiresAt?.let(Instant::parse)?.takeIf { !it.isAfter(clock()) }

    private fun refused(status: Int) = EnrollmentException(when (status) {
        403 -> "Use the server's private LAN address, with device pairing on."
        404 -> "This server doesn't offer mobile enrollment."
        409 -> "The server refused this step. Ask the owner to start a new pairing."
        410 -> "This pairing expired or was already used. Ask the owner for a new link."
        422 -> "The server refused this phone's claim."
        429 -> "Too many attempts. Wait ten minutes."
        else -> "The server answered $status."
    }, status)

    private fun hex(bytes: Int) = ByteArray(bytes).also { SecureRandom().nextBytes(it) }.joinToString("") { "%02x".format(it) }

    companion object {
        /** Every identity key this client makes; nothing else in the app's Keystore may use it. */
        const val ALIAS_PREFIX = "rynhult-identity-"
        private val JSON = mapOf("Content-Type" to "application/json")
    }
}
