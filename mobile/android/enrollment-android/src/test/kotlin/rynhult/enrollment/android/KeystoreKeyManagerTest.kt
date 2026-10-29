package rynhult.enrollment.android

import rynhult.enrollment.EnrollmentState
import rynhult.enrollment.MemoryStateStore
import java.security.PrivateKey
import java.security.cert.CertificateFactory
import java.security.cert.X509Certificate
import kotlin.test.Test
import kotlin.test.assertContentEquals
import kotlin.test.assertEquals
import kotlin.test.assertNull
import kotlin.test.assertSame

class KeystoreKeyManagerTest {
    private val identity = EnrollmentState("https://example.com", "installation", alias = "current", deviceId = "device")
    private val store = MemoryStateStore(identity)
    private val chain = KeystoreKeyManagerTest::class.java.getResourceAsStream("/public-test-chain.txt")!!.use { stream ->
        CertificateFactory.getInstance("X.509").generateCertificates(stream)
            .map { it as X509Certificate }.toTypedArray()
    }
    // Like an Android Keystore handle, this key cannot be exported.
    private val key = object : PrivateKey {
        override fun getAlgorithm() = "EC"
        override fun getFormat(): String? = null
        override fun getEncoded(): ByteArray? = null
    }
    private val keys = object : KeyManagerKeys {
        override fun chain(alias: String): Array<X509Certificate>? = if (alias == "current") this@KeystoreKeyManagerTest.chain else null
        override fun privateKey(alias: String): PrivateKey? = if (alias == "current") this@KeystoreKeyManagerTest.key else null
    }
    private val manager = KeystoreKeyManager(keys, store)

    @Test fun `current identity supplies the full certificate chain and opaque key`() {
        val alias = manager.chooseClientAlias(arrayOf("EC"), null, null)
        assertEquals("current", alias)
        assertEquals(2, chain.size)
        assertContentEquals(chain, manager.getCertificateChain(alias))
        assertSame(key, manager.getPrivateKey(alias))
        assertEquals("current", manager.chooseEngineClientAlias(arrayOf("EC"), null, null))
    }

    @Test fun `rotation between selection and lookup refuses the previous identity`() {
        val alias = manager.chooseClientAlias(arrayOf("EC"), null, null)
        assertEquals("current", alias)
        store.save(identity.copy(alias = "replacement"))
        assertNull(manager.getCertificateChain(alias))
        assertNull(manager.getPrivateKey(alias))
    }

    @Test fun `rotation after chain lookup refuses the old private key`() {
        val alias = manager.chooseClientAlias(arrayOf("EC"), null, null)
        assertEquals("current", alias)
        assertContentEquals(chain, manager.getCertificateChain(alias))
        store.save(identity.copy(alias = "replacement"))
        assertNull(manager.getPrivateKey(alias))
    }

    @Test fun `revocation between selection and lookup refuses the previous identity`() {
        val alias = manager.chooseClientAlias(arrayOf("EC"), null, null)
        assertEquals("current", alias)
        store.clear()
        assertNull(manager.getCertificateChain(alias))
        assertNull(manager.getPrivateKey(alias))
        assertNull(manager.chooseClientAlias(arrayOf("EC"), null, null))
    }

    @Test fun `unfinished claim refuses the existing identity`() {
        store.save(identity.copy(pendingAlias = "staged"))
        assertNull(manager.chooseClientAlias(arrayOf("EC"), null, null))
        assertNull(manager.getCertificateChain("current"))
        assertNull(manager.getPrivateKey("current"))
    }

    @Test fun `unknown and null aliases return nothing`() {
        assertNull(manager.getCertificateChain("unknown"))
        assertNull(manager.getPrivateKey("unknown"))
        assertNull(manager.getCertificateChain(null))
        assertNull(manager.getPrivateKey(null))
        // Even the saved alias must have material in the Keystore.
        store.save(identity.copy(alias = "missing"))
        assertNull(manager.getCertificateChain("missing"))
        assertNull(manager.getPrivateKey("missing"))
    }
}
