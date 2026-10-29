package rynhult.enrollment

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import java.math.BigInteger
import java.security.AlgorithmParameters
import java.security.KeyFactory
import java.security.KeyPair
import java.security.KeyPairGenerator
import java.security.MessageDigest
import java.security.PublicKey
import java.security.Signature
import java.security.cert.X509Certificate
import java.security.spec.ECGenParameterSpec
import java.security.spec.ECParameterSpec
import java.security.spec.ECPrivateKeySpec
import java.security.spec.X509EncodedKeySpec
import java.time.Instant
import java.time.OffsetDateTime
import java.util.Base64
import kotlin.test.assertEquals
import kotlin.test.assertTrue

/**
 * The recorded server exchanges (src/test/resources/fixtures/lifecycle.json, made by tools/record_fixtures.py from
 * the server branch's own test fixtures). Their keys are derived from fixed test labels, so the same keys can be
 * rebuilt here; they are test-only and valid nowhere else.
 */
object Fixtures {
    val root: JsonObject = Json.parseToJsonElement(
        Fixtures::class.java.getResource("/fixtures/lifecycle.json")!!.readText()).jsonObject

    val origin = root.string("private_origin")
    val installationId = root.string("installation_id")
    val now: Instant = OffsetDateTime.parse(root.string("now_for_tests")).toInstant()
    val cookieSecure = root["cookie_secure"]!!.jsonPrimitive.content.toBooleanStrict()
    const val PERSON = "casey"  // the person the recorded pairing was for

    /** The device-issuer CA fingerprint an owner would approve: the recorded test CA's. */
    val issuer: String by lazy {
        CredentialValidator.sha256Hex(CredentialValidator.certificates(credential("credential").chain).single().encoded)
    }

    fun exchange(name: String): JsonObject = root["exchanges"]!!.jsonObject[name]?.jsonObject ?: error("no exchange $name")

    fun response(name: String): HttpResponse {
        val exchange = exchange(name)
        val body = exchange["body"]?.toString() ?: exchange.string("text")
        val cookies = exchange["set_cookie"]?.jsonArray?.map { it.jsonPrimitive.content } ?: emptyList()
        return HttpResponse(exchange["status"]!!.jsonPrimitive.content.toInt(), body, cookies)
    }

    fun body(name: String): JsonObject = exchange(name)["body"]!!.jsonObject
    fun link(name: String) = body(name).string("link")
    fun credential(name: String) = Api.json.decodeFromString(CredentialResponse.serializer(), response(name).body)

    /** The fixture's test key `index` (0 or 1), rebuilt from its label; checked against the recorded public key. */
    fun keyPair(index: Int): KeyPair {
        val entry = root["test_keys"]!!.jsonArray[index].jsonObject
        val scalar = BigInteger(1, MessageDigest.getInstance("SHA-256").digest(entry.string("label").toByteArray()))
        val params = AlgorithmParameters.getInstance("EC").apply { init(ECGenParameterSpec("secp256r1")) }
            .getParameterSpec(ECParameterSpec::class.java)
        val factory = KeyFactory.getInstance("EC")
        val privateKey = factory.generatePrivate(ECPrivateKeySpec(scalar, params))
        val publicKey = factory.generatePublic(X509EncodedKeySpec(Base64.getDecoder().decode(entry.string("public_key_spki_b64"))))
        val probe = "fixture key check".toByteArray()
        val signature = Signature.getInstance("SHA256withECDSA").run { initSign(privateKey); update(probe); sign() }
        assertTrue(verify(publicKey, probe, signature), "the rebuilt private key matches the recorded public key")
        return KeyPair(publicKey, privateKey)
    }

    fun verify(key: PublicKey, data: ByteArray, signature: ByteArray) =
        Signature.getInstance("SHA256withECDSA").run { initVerify(key); update(data); verify(signature) }

    private fun JsonObject.string(key: String) = (this[key] as JsonPrimitive).content
}

/** Software stand-in for the Keystore: same contract, keys in memory. [next] queues the keys `create` returns. */
class SoftwareKeys : DeviceKeys {
    val keys = mutableMapOf<String, KeyPair>()
    val chains = mutableMapOf<String, List<X509Certificate>>()
    val next = ArrayDeque<KeyPair>()

    override fun create(alias: String): PublicKey {
        check(alias !in keys) { "alias reused" }
        val pair = next.removeFirstOrNull()
            ?: KeyPairGenerator.getInstance("EC").apply { initialize(ECGenParameterSpec("secp256r1")) }.generateKeyPair()
        keys[alias] = pair
        return pair.public
    }

    override fun sign(alias: String, data: ByteArray): ByteArray =
        Signature.getInstance("SHA256withECDSA").run { initSign(keys.getValue(alias).private); update(data); sign() }

    override fun publicKey(alias: String) = keys[alias]?.public
    override fun storeChain(alias: String, chain: List<X509Certificate>) { chains[alias] = chain }
    override fun delete(alias: String) { keys.remove(alias); chains.remove(alias) }
    override fun aliases(prefix: String) = keys.keys.filter { it.startsWith(prefix) }
}

/**
 * Plays the recorded server answers in order, checking each request: method and path, and for claims that the
 * body is one the server accepts (the proof verifies with the CSR's key over the documented message).
 */
class ReplayTransport(vararg steps: Pair<String, String>) : Transport {
    private val steps = ArrayDeque(steps.toList())
    val requests = mutableListOf<Triple<String, String, String?>>()

    override fun send(method: String, path: String, jsonBody: String?, headers: Map<String, String>): HttpResponse {
        val (expected, name) = steps.removeFirstOrNull() ?: error("unexpected request $method $path")
        val recorded = Fixtures.exchange(name)["request"]!!.jsonObject
        assertEquals(expected, "$method $path")
        assertEquals("${(recorded["method"] as JsonPrimitive).content} ${(recorded["path"] as JsonPrimitive).content}", "$method $path")
        requests += Triple(method, path, jsonBody)
        if (path.endsWith("/claim")) checkClaim(jsonBody!!, recorded["body"]!!.jsonObject)
        return Fixtures.response(name)
    }

    fun done() = assertTrue(steps.isEmpty(), "unplayed steps: $steps")

    private fun checkClaim(sent: String, recorded: JsonObject) {
        val claim = Api.json.decodeFromString(ClaimRequest.serializer(), sent)
        assertEquals((recorded["token"] as JsonPrimitive).content, claim.token)
        assertEquals(Fixtures.installationId, claim.installationId)
        assertEquals("android", claim.platform)
        assertTrue(claim.csr.toByteArray().size <= 2048)
        val key = CsrReader.publicKey(claim.csr)  // also checks the CSR's own signature
        val pairing = Fixtures.root["exchanges"]!!.jsonObject.values
            .map { it.jsonObject["body"] }.filterIsInstance<JsonObject>()
            .mapNotNull { (it["link"] as? JsonPrimitive)?.content }
            .first { it.endsWith(":" + claim.token) }
        val link = PairingLink.parse(pairing, Fixtures.origin)
        val message = ClaimProof.message(link, claim.installationId, claim.csr)
        assertTrue(Fixtures.verify(key, message, Base64.getUrlDecoder().decode(claim.proof)), "claim proof verifies")
        assertTrue(!claim.proof.contains('=') && !claim.proof.contains('+') && !claim.proof.contains('/'))
    }
}

/** Just enough DER reading to check a PKCS#10 request: its signature over the request info, and its key. */
object CsrReader {
    fun publicKey(pem: String): PublicKey {
        val der = Base64.getMimeDecoder().decode(pem.substringAfter("-----BEGIN CERTIFICATE REQUEST-----")
            .substringBefore("-----END CERTIFICATE REQUEST-----"))
        val (request, _) = read(der, 0)
        val (info, afterInfo) = read(request.value, 0)
        val (_, afterAlgorithm) = read(request.value, afterInfo)
        val (signature, _) = read(request.value, afterAlgorithm)
        var at = 0
        val (version, v) = read(info.value, at); at = v
        assertEquals(listOf<Byte>(0), version.value.toList())
        val (_, n) = read(info.value, at); at = n
        val (spki, _) = read(info.value, at)
        val key = KeyFactory.getInstance("EC").generatePublic(X509EncodedKeySpec(spki.whole))
        assertEquals(0, signature.value[0].toInt(), "no unused bits")
        assertTrue(Fixtures.verify(key, info.whole, signature.value.copyOfRange(1, signature.value.size)), "CSR self-signature")
        return key
    }

    class Tlv(val whole: ByteArray, val value: ByteArray)

    private fun read(bytes: ByteArray, start: Int): Pair<Tlv, Int> {
        var at = start + 1
        var length = bytes[at++].toInt() and 0xFF
        if (length >= 0x80) {
            val count = length and 0x7F
            length = 0
            repeat(count) { length = (length shl 8) or (bytes[at++].toInt() and 0xFF) }
        }
        val end = at + length
        return Tlv(bytes.copyOfRange(start, end), bytes.copyOfRange(at, end)) to end
    }
}
