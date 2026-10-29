package rynhult.enrollment.android

import rynhult.enrollment.ClientIdentity
import rynhult.enrollment.StateStore
import java.net.Socket
import java.security.Principal
import java.security.PrivateKey
import java.security.cert.X509Certificate
import javax.net.ssl.SSLEngine
import javax.net.ssl.X509ExtendedKeyManager

/** The two Keystore lookups TLS needs; the returned private key remains an opaque signing handle. */
internal interface KeyManagerKeys {
    fun chain(alias: String): Array<X509Certificate>?
    fun privateKey(alias: String): PrivateKey?
}

/**
 * Presents the current enrolled identity for TLS client authentication, with the private key used inside the
 * Keystore (the TLS stack signs through it; the key never leaves). It reads the current alias on every handshake, so
 * a rotation or revocation takes effect on the next connection; EnrollmentClient's onIdentityChanged must still drop
 * pooled connections, which would otherwise keep the previous identity.
 *
 * It offers the identity only to a server that asks for an ECDSA key and accepts the device CA (or names no issuers);
 * it never chooses a server identity.
 */
class KeystoreKeyManager internal constructor(
    private val keys: KeyManagerKeys,
    private val store: StateStore
) : X509ExtendedKeyManager() {
    constructor(keys: AndroidKeystoreKeys, store: StateStore) : this(object : KeyManagerKeys {
        override fun chain(alias: String): Array<X509Certificate>? = keys.chain(alias)
        override fun privateKey(alias: String): PrivateKey? = keys.privateKey(alias)
    }, store)

    // Nothing presents the identity while a claim is unfinished (docs/MOBILE-APPS.md, client lifecycle rules).
    private fun current(): String? = store.load()?.takeIf { it.enrolled && it.pendingAlias == null }?.alias

    override fun chooseClientAlias(keyType: Array<out String>?, issuers: Array<out Principal>?, socket: Socket?): String? =
        current()?.takeIf { keyType?.any(::ecdsa) == true && accepts(it, issuers) }

    override fun chooseEngineClientAlias(keyType: Array<out String>?, issuers: Array<out Principal>?, engine: SSLEngine?): String? =
        chooseClientAlias(keyType, issuers, null)

    override fun getClientAliases(keyType: String?, issuers: Array<out Principal>?): Array<String>? =
        current()?.takeIf { keyType != null && ecdsa(keyType) && accepts(it, issuers) }?.let { arrayOf(it) }

    private fun accepts(alias: String, issuers: Array<out Principal>?): Boolean =
        ClientIdentity.offeredTo(keys.chain(alias)?.getOrNull(1)?.subjectX500Principal, issuers)

    override fun getCertificateChain(alias: String?): Array<X509Certificate>? =
        alias?.takeIf { it == current() }?.let(keys::chain)

    override fun getPrivateKey(alias: String?): PrivateKey? =
        alias?.takeIf { it == current() }?.let(keys::privateKey)

    // "EC" in TLS 1.3 and for ecdsa_sign; JSSE's TLS 1.2 names look like "EC_EC".
    private fun ecdsa(keyType: String) = keyType == "EC" || keyType.startsWith("EC_")

    override fun chooseServerAlias(keyType: String?, issuers: Array<out Principal>?, socket: Socket?): String? = null
    override fun getServerAliases(keyType: String?, issuers: Array<out Principal>?): Array<String>? = null
}
