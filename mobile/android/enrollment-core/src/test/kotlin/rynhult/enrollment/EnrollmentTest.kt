package rynhult.enrollment

import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import java.nio.file.Files
import java.time.Duration
import java.time.Instant
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertFalse
import kotlin.test.assertIs
import kotlin.test.assertNotEquals
import kotlin.test.assertNull
import kotlin.test.assertTrue

private const val CLAIM = "POST /api/mobile/v1/claim"
private const val CREDENTIAL = "POST /api/mobile/v1/credential"
private const val AUTH = "GET /api/auth/state"
private const val BIND = "POST /api/mobile/v1/session/bind"
private const val DEVICES = "GET /api/mobile/v1/devices"
private const val PUBLIC = "http://remote.example.com"  // the fixtures run without TLS; production is https

class EnrollmentTest {
    private fun fresh() = MemoryStateStore(EnrollmentState(Fixtures.origin, Fixtures.installationId))

    private class Run(val keys: SoftwareKeys, val store: StateStore) {
        var changed = 0
        var now: Instant = Fixtures.now
        fun client(transport: Transport, person: String = Fixtures.PERSON, issuer: String = Fixtures.issuer) =
            EnrollmentClient(Fixtures.origin, person, issuer, transport, keys, store, { now }, { changed++ })
    }

    private fun run() = Run(SoftwareKeys(), fresh())
    private val session = Session("signed-in", "csrf-before", secure = Fixtures.cookieSecure)

    /** The recorded first enrollment, with fixture key 1. */
    private fun enroll(run: Run): CredentialOutcome.Enrolled {
        run.keys.next += Fixtures.keyPair(0)
        val replay = ReplayTransport(CLAIM to "claim", CREDENTIAL to "credential_waiting", CREDENTIAL to "credential")
        val client = run.client(replay)
        val pending = client.claim(Fixtures.link("start"))
        assertEquals((Fixtures.body("claim")["matching_code"] as JsonPrimitive).content, pending.matchingCode)
        assertEquals(CredentialOutcome.Waiting, client.credential(pending))
        val enrolled = assertIs<CredentialOutcome.Enrolled>(client.credential(pending))
        replay.done()
        return enrolled
    }

    /** Enrolled, then the recorded person check and binding. */
    private fun bound(run: Run): Session {
        enroll(run)
        val replay = ReplayTransport(AUTH to "auth_state", BIND to "bind")
        val bound = run.client(replay).bindSession(replay, PUBLIC, session)
        replay.done()
        return bound
    }

    /** The recorded listing with this device's fields changed, for the listing rule. */
    private fun listing(change: (MutableMap<String, kotlinx.serialization.json.JsonElement>) -> Unit): HttpResponse {
        val device = Fixtures.body("devices_public")["devices"]!!.jsonArray[0].jsonObject.toMutableMap().also(change)
        val body = buildJsonObject { put("devices", kotlinx.serialization.json.JsonArray(listOf(JsonObject(device)))) }
        return HttpResponse(200, body.toString())
    }

    @Test
    fun `enrolls through the recorded server flow and stores the validated chain`() {
        val run = run()
        val enrolled = enroll(run)
        val state = run.store.load()!!
        assertEquals(Fixtures.credential("credential").deviceId, enrolled.deviceId)
        assertEquals(Activation.PENDING, enrolled.activation)
        assertFalse(enrolled.rotated)
        assertEquals(state.deviceId, enrolled.deviceId)
        assertEquals(Fixtures.issuer, state.caSha256)
        assertFalse(state.bound)
        assertNull(state.pendingAlias)
        val leaf = run.keys.chains.getValue(state.alias!!)[0]
        assertEquals(CredentialValidator.sha256Hex(leaf.encoded), state.fingerprint)
        assertEquals(setOf(state.alias), run.keys.keys.keys)
        assertEquals(Instant.parse(state.expiresAt), leaf.notAfter.toInstant())
        assertEquals(1, run.changed)
    }

    @Test
    fun `the device CA is approved up front, never learned from the first credential`() {
        val run = run()
        run.keys.next += Fixtures.keyPair(0)
        val client = run.client({ _, path, _, _ ->
            if (path.endsWith("/claim")) Fixtures.response("claim") else Fixtures.response("credential")
        }, issuer = "0".repeat(64))
        val pending = client.claim(Fixtures.link("start"))
        assertFailsWith<EnrollmentException> { client.credential(pending) }
        assertTrue(run.keys.keys.isEmpty())
        assertNull(run.store.load()!!.alias)
        assertFailsWith<EnrollmentException> { run.client({ _, _, _, _ -> error("unused") }, issuer = "not a fingerprint") }
    }

    @Test
    fun `rotation keeps the working identity until the switch, then requires a new binding`() {
        val run = run()
        bound(run)
        val before = run.store.load()!!
        run.keys.next += Fixtures.keyPair(1)
        val replay = ReplayTransport(CLAIM to "rotate_claim", CREDENTIAL to "rotate_credential")
        val client = run.client(replay)
        val pending = client.claim(Fixtures.link("rotate"))
        // While claiming: the old key is kept, unused and unbound; nothing can bind.
        val claiming = run.store.load()!!
        assertEquals(before.alias, claiming.alias)
        assertEquals(pending.alias, claiming.pendingAlias)
        assertFalse(claiming.bound)
        assertEquals(DeviceStatus.NotBound, client.status({ _, _, _, _ -> error("not asked") }, PUBLIC, session))
        assertFailsWith<EnrollmentException> { client.bindSession({ _, _, _, _ -> error("not asked") }, PUBLIC, session) }
        val rotated = assertIs<CredentialOutcome.Enrolled>(client.credential(pending))
        replay.done()
        val after = run.store.load()!!
        assertTrue(rotated.rotated)
        assertEquals(before.deviceId, after.deviceId)
        assertEquals(before.caSha256, after.caSha256)
        assertEquals(before.installationId, after.installationId)
        assertNotEquals(before.alias, after.alias)
        assertNotEquals(before.fingerprint, after.fingerprint)
        assertFalse(after.bound)
        assertNull(after.pendingAlias)
        assertEquals(setOf(after.alias), run.keys.keys.keys, "the previous key is deleted at the switch")
        assertTrue(run.changed >= 3, "claim and switch drop sessions and pooled connections")
    }

    @Test
    fun `a refused or abandoned rotation returns to the working identity`() {
        val run = run()
        bound(run)
        val before = run.store.load()!!
        val refusing = run.client(transport = Transport { _, _, _, _ -> Fixtures.response("rotate_same_key") })
        assertEquals(409, assertFailsWith<EnrollmentException> { refusing.claim(Fixtures.link("rotate")) }.status)
        val afterRefusal = run.store.load()!!
        assertEquals(before.alias, afterRefusal.alias)
        assertNull(afterRefusal.pendingAlias)
        assertFalse(afterRefusal.bound, "the session ended with the claim")
        assertEquals(setOf(before.alias), run.keys.keys.keys)

        // Abandoned: the app locked while the owner typed the code. recover() drops only the staged key.
        val claiming = run.client(transport = Transport { _, _, _, _ -> Fixtures.response("rotate_claim") })
        claiming.claim(Fixtures.link("rotate"))
        assertEquals(2, run.keys.keys.size)
        claiming.recover()
        assertEquals(setOf(before.alias), run.keys.keys.keys)
        assertNull(run.store.load()!!.pendingAlias)
    }

    @Test
    fun `a rotated credential that fails validation wipes everything`() {
        val run = run()
        bound(run)
        run.keys.next += Fixtures.keyPair(1)
        val client = run.client(transport = Transport { _, path, _, _ ->
            if (path.endsWith("/claim")) Fixtures.response("rotate_claim") else Fixtures.response("credential")  // the old leaf
        })
        val pending = client.claim(Fixtures.link("rotate"))
        assertFailsWith<EnrollmentException> { client.credential(pending) }
        assertTrue(run.keys.keys.isEmpty())
        assertNull(run.store.load()!!.alias)
    }

    @Test
    fun `refused claims leave no key behind`() {
        for ((name, status) in listOf("claim_used" to 410, "rotate_same_key" to 409, "throttled" to 429)) {
            val run = run()
            val client = run.client(transport = Transport { _, _, _, _ -> Fixtures.response(name) })
            val error = assertFailsWith<EnrollmentException> { client.claim(Fixtures.link("start")) }
            assertEquals(status, error.status)
            assertTrue(run.keys.keys.isEmpty(), name)
            assertNull(run.store.load()!!.alias)
            assertNull(run.store.load()!!.pendingAlias)
        }
    }

    @Test
    fun `an expired pairing deletes its key, and throttling waits`() {
        val run = run()
        run.keys.next += Fixtures.keyPair(0)
        val replay = ReplayTransport(CLAIM to "claim", CREDENTIAL to "throttled", CREDENTIAL to "credential_after_revoke")
        val client = run.client(replay)
        val pending = client.claim(Fixtures.link("start"))
        assertEquals(CredentialOutcome.Throttled, client.credential(pending))
        assertEquals(CredentialOutcome.Expired, client.credential(pending))
        assertTrue(run.keys.keys.isEmpty())
        assertNull(run.store.load()!!.pendingAlias)
        replay.done()
    }

    @Test
    fun `a credential that fails validation is never stored`() {
        val run = run()
        run.keys.next += Fixtures.keyPair(1)  // not the key the recorded leaf was issued for
        val client = run.client(transport = Transport { _, path, _, _ ->
            if (path.endsWith("/claim")) Fixtures.response("claim") else Fixtures.response("credential")
        })
        val pending = client.claim(Fixtures.link("start"))
        assertFailsWith<EnrollmentException> { client.credential(pending) }
        assertTrue(run.keys.keys.isEmpty() && run.keys.chains.isEmpty())
        assertNull(run.store.load()!!.alias)
        assertEquals(0, run.changed)
    }

    @Test
    fun `the validator checks key, CA, device, profile and time like the server does`() {
        val issued = Fixtures.credential("credential")
        val key = Fixtures.keyPair(0).public
        val identity = CredentialValidator.validate(issued, key, Fixtures.now, Fixtures.issuer, null)
        CredentialValidator.validate(Fixtures.credential("rotate_credential"), Fixtures.keyPair(1).public, Fixtures.now,
                                     identity.caSha256, issued.deviceId)
        val refusals = mapOf(
            "another key" to { CredentialValidator.validate(issued, Fixtures.keyPair(1).public, Fixtures.now, null, null) },
            "another CA" to { CredentialValidator.validate(issued, key, Fixtures.now, "0".repeat(64), null) },
            "another device" to { CredentialValidator.validate(issued, key, Fixtures.now, null, "f".repeat(32)) },
            "leaf and CA swapped" to {
                CredentialValidator.validate(issued.copy(certificate = issued.chain, chain = issued.certificate), key, Fixtures.now, null, null)
            },
            "two leaves" to {
                CredentialValidator.validate(issued.copy(certificate = issued.certificate + issued.certificate), key, Fixtures.now, null, null)
            },
            "an expiry the leaf doesn't have" to {
                CredentialValidator.validate(issued.copy(expiresAt = "2030-01-01T00:00:00+00:00"), key, Fixtures.now, null, null)
            },
            "before it is valid" to { CredentialValidator.validate(issued, key, Fixtures.now.minus(Duration.ofDays(1)), null, null) },
            "after it expired" to {
                CredentialValidator.validate(issued, key, identity.leaf.notAfter.toInstant().plusSeconds(1), null, null)
            },
            "not a certificate" to {
                CredentialValidator.validate(issued.copy(certificate = "TEST PUBLIC LEAF"), key, Fixtures.now, null, null)
            },
        )
        for ((case, attempt) in refusals) assertFailsWith<EnrollmentException>(case) { attempt() }
    }

    @Test
    fun `binding checks the person first, then reads the rotated session from the recorded answer`() {
        val run = run()
        val bound = bound(run)
        val recorded = Fixtures.response("bind").setCookies.first { it.startsWith("acc_session=") }
        assertEquals(recorded.substringAfter("=").substringBefore(";"), bound.token)
        assertFalse(bound.toString().contains(bound.token))
        assertTrue(run.store.load()!!.bound)
    }

    @Test
    fun `binding refuses another person or a pending step-up without binding`() {
        val run = run()
        enroll(run)
        val replay = ReplayTransport(AUTH to "auth_state")  // no bind request may follow
        assertFailsWith<EnrollmentException> { run.client(replay, person = "dana").bindSession(replay, PUBLIC, session) }
        replay.done()
        val stepUp = Fixtures.response("auth_state").let { it.copy(body = it.body.replace("\"step_up_required\":false", "\"step_up_required\":true")) }
        assertTrue(stepUp.body.contains("\"step_up_required\":true"))
        assertFailsWith<EnrollmentException> {
            run.client(transport = Transport { _, _, _, _ -> error("unused") }).bindSession({ _, path, _, _ ->
                if (path == "/api/auth/state") stepUp else error("no bind after a refused person check")
            }, PUBLIC, session)
        }
        assertFalse(run.store.load()!!.bound)
        assertTrue(run.store.load()!!.enrolled)
    }

    @Test
    fun `binding refusals - 409 waits, 403 ends only the session, 404 and 410 forget`() {
        for ((status, forgotten) in listOf(409 to false, 403 to false, 404 to true, 410 to true)) {
            val run = run()
            enroll(run)
            val error = assertFailsWith<EnrollmentException> {
                run.client(transport = Transport { _, _, _, _ -> error("unused") }).bindSession({ _, path, _, _ ->
                    if (path == "/api/auth/state") Fixtures.response("auth_state") else HttpResponse(status, "{}")
                }, PUBLIC, session)
            }
            assertEquals(status, error.status)
            assertEquals(forgotten, !run.store.load()!!.enrolled, "status $status")
            assertFalse(run.store.load()!!.bound)
        }
    }

    @Test
    fun `status - active only for exactly this device, and a 401 ends only the binding`() {
        val run = run()
        bound(run)
        val client = run.client(transport = Transport { _, _, _, _ -> error("no LAN request expected") })
        assertIs<DeviceStatus.Active>(client.status(ReplayTransport(DEVICES to "devices_public"), PUBLIC, session))
        assertEquals(DeviceStatus.SignedOut, client.status(ReplayTransport(DEVICES to "session_after_revoke"), PUBLIC, session))
        assertTrue(run.store.load()!!.enrolled)
        assertEquals(DeviceStatus.NotBound, client.status({ _, _, _, _ -> error("not asked while unbound") }, PUBLIC, session))
    }

    @Test
    fun `status forgets the identity on the server's word - listing mismatches and 403, 404, 410`() {
        val mismatches: List<(MutableMap<String, kotlinx.serialization.json.JsonElement>) -> Unit> = listOf(
            { it["person"] = JsonPrimitive("dana") },
            { it["kind"] = JsonPrimitive("browser") },
            { it["status"] = JsonPrimitive("expired") },
            { it["activation"] = JsonPrimitive("inactive") },
            { it["fingerprint"] = JsonPrimitive("0".repeat(64)) },
            { it["expires_at"] = JsonPrimitive("2030-01-01T00:00:00+00:00") },
            { it["id"] = JsonPrimitive("f".repeat(32)) },
        )
        val answers = mismatches.map { listing(it) } + listOf(
            Fixtures.response("devices_after_revoke"),   // recorded: revoked, inactive
            Fixtures.response("edge_after_revoke"),      // recorded: the edge's 403 for a revoked leaf
            HttpResponse(404, "{}"), HttpResponse(410, "{}"),
        )
        for ((index, answer) in answers.withIndex()) {
            val run = run()
            bound(run)
            val before = run.store.load()!!
            assertEquals(DeviceStatus.Revoked, run.client(transport = Transport { _, _, _, _ -> error("unused") })
                .status({ _, _, _, _ -> answer }, PUBLIC, session), "case $index")
            val after = run.store.load()!!
            assertTrue(run.keys.keys.isEmpty(), "case $index")
            assertNull(after.alias)
            assertNotEquals(before.installationId, after.installationId)
        }
    }

    @Test
    fun `renewal warnings at 14, 7 and 1 days, and expiry forgets without asking the server`() {
        val run = run()
        enroll(run)
        val expires = Instant.parse(run.store.load()!!.expiresAt)
        val client = run.client(transport = Transport { _, _, _, _ -> error("no request expected") })
        val cases = mapOf(Duration.ofDays(20) to null, Duration.ofDays(13) to 14, Duration.ofDays(6) to 7, Duration.ofHours(12) to 1)
        for ((left, warning) in cases) {
            run.now = expires.minus(left)
            assertEquals(warning, client.renewWithinDays(), left.toString())
        }
        run.now = expires.plusSeconds(1)
        assertEquals(DeviceStatus.Expired, client.status({ _, _, _, _ -> error("not asked") }, PUBLIC, Session("t", "c")))
        assertTrue(run.keys.keys.isEmpty())
    }

    @Test
    fun `secrets never appear in descriptions`() {
        val claim = ClaimRequest("token-secret", Fixtures.installationId, "android", "csr", "proof-secret")
        val texts = listOf(
            claim.toString(), ClaimResponse("receipt-secret", "123456").toString(), ReceiptRequest("receipt-secret").toString(),
            HttpResponse(200, "{\"receipt\":\"receipt-secret\"}", listOf("acc_session=session-secret")).toString(),
            Session("session-secret", "csrf-secret").toString(),
        )
        for (text in texts) {
            for (secret in listOf("token-secret", "proof-secret", "receipt-secret", "session-secret", "csrf-secret")) {
                assertFalse(text.contains(secret), "$secret in $text")
            }
        }
    }

    @Test
    fun `pairing links must name the chosen server exactly`() {
        val link = Fixtures.link("start")
        val parsed = PairingLink.parse(link, Fixtures.origin)
        assertEquals(Fixtures.origin, parsed.origin)
        assertFalse(parsed.toString().contains(parsed.token))
        val fragment = link.substringAfter("#")
        val refused = listOf(
            "https://acc.example.com/pair-mobile#$fragment",       // another server
            "http://acc.example.lan/pair-mobile#$fragment",        // not HTTPS
            "https://acc.example.lan/pair-mobile?x=1#$fragment",   // a query
            "https://user@acc.example.lan/pair-mobile#$fragment",  // user info
            "https://acc.example.lan/pair#$fragment",              // another path
            "https://acc.example.lan/pair-mobile#${fragment.substringBefore(":")}", // no token
            "https://acc.example.lan/pair-mobile",                 // no fragment
        )
        for (text in refused) assertFailsWith<EnrollmentException>(text) { PairingLink.parse(text, Fixtures.origin) }
        assertEquals(Fixtures.origin, PairingLink.parse(link.replace("acc.example.lan", "ACC.example.lan:443"), Fixtures.origin).origin)
    }

    @Test
    fun `claim proof message is the documented one`() {
        val link = PairingLink.parse(Fixtures.link("start"), Fixtures.origin)
        val csr = "-----BEGIN CERTIFICATE REQUEST-----\nAA==\n-----END CERTIFICATE REQUEST-----\n"
        val message = String(ClaimProof.message(link, Fixtures.installationId, csr), Charsets.UTF_8)
        assertEquals(listOf("acc-mobile-claim-v1", link.pairingId, link.token, Fixtures.origin, Fixtures.installationId,
                            "android", Csr.sha256Hex(csr)).joinToString("\n"), message)
        assertFalse(message.endsWith("\n"))
        assertEquals("_-8", ClaimProof.encode(byteArrayOf(0xFF.toByte(), 0xEF.toByte())))
    }

    @Test
    fun `the CSR is a signed P-256 request within the server's size limit`() {
        val keys = SoftwareKeys()
        val publicKey = keys.create("a")
        val pem = Csr.pem(publicKey) { keys.sign("a", it) }
        assertTrue(pem.startsWith("-----BEGIN CERTIFICATE REQUEST-----\n") && pem.endsWith("-----END CERTIFICATE REQUEST-----\n"))
        assertTrue(pem.lines().all { it.length <= 64 })
        assertTrue(pem.toByteArray().size <= 2048)
        assertTrue(CsrReader.publicKey(pem).encoded.contentEquals(publicKey.encoded))
    }

    @Test
    fun `state survives in a file, is per server, and installation IDs are fresh`() {
        val directory = Files.createTempDirectory("rynhult-enrollment").toFile()
        try {
            val store = FileStateStore(directory.resolve("state.json"))
            val state = EnrollmentState(Fixtures.origin, Fixtures.installationId, "alias", "d".repeat(32), "c".repeat(64),
                                        "e".repeat(64), "2027-01-01T00:00:00Z", bound = true)
            store.save(state)
            assertEquals(state, FileStateStore(directory.resolve("state.json")).load())
            val other = EnrollmentClient("https://other.example.lan", Fixtures.PERSON, Fixtures.issuer,
                                         { _, _, _, _ -> error("unused") }, SoftwareKeys(), store)
            assertFailsWith<EnrollmentException> { other.state }
            assertNotEquals(EnrollmentState.newInstallationId(), EnrollmentState.newInstallationId())
        } finally {
            directory.deleteRecursively()
        }
    }

    @Test
    fun `an abandoned first pairing starts a new installation, a rotation keeps its own`() {
        // The owner may have confirmed before the phone collected its credential: the server's device record then
        // holds that installation ID and would refuse it on every later pairing ("already enrolled").
        val run = run()
        val before = run.store.load()!!.installationId
        run.client(transport = Transport { _, _, _, _ -> Fixtures.response("claim") }).apply { claim(Fixtures.link("start")); recover() }
        val afterAbandon = run.store.load()!!
        assertNotEquals(before, afterAbandon.installationId)
        assertNull(afterAbandon.pendingAlias)
        assertTrue(run.keys.keys.isEmpty())

        val rotating = run()
        bound(rotating)
        val enrolled = rotating.store.load()!!.installationId
        rotating.client(transport = Transport { _, _, _, _ -> Fixtures.response("rotate_claim") }).apply { claim(Fixtures.link("rotate")); recover() }
        assertEquals(enrolled, rotating.store.load()!!.installationId, "rotation needs this installation")
    }

    @Test
    fun `a first credential that fails validation also starts a new installation`() {
        val run = run()
        val before = run.store.load()!!.installationId
        run.keys.next += Fixtures.keyPair(1)  // not the key the recorded leaf was issued for
        val client = run.client(transport = Transport { _, path, _, _ ->
            if (path.endsWith("/claim")) Fixtures.response("claim") else Fixtures.response("credential")
        })
        assertFailsWith<EnrollmentException> { client.credential(client.claim(Fixtures.link("start"))) }
        assertNotEquals(before, run.store.load()!!.installationId)
    }

    @Test
    fun `a malformed answer never carries the body into the exception`() {
        val run = run()
        val client = run.client(transport = Transport { _, _, _, _ -> HttpResponse(200, "{\"receipt\": \"receipt-secret\", \"matching_code\": 12") })
        val error = assertFailsWith<EnrollmentException> { client.claim(Fixtures.link("start")) }
        assertFalse(error.toString().contains("receipt-secret"))
        assertNull(error.cause)
        assertTrue(run.keys.keys.isEmpty())
    }

    @Test
    fun `recover deletes identity keys the record no longer names`() {
        // A crash between the rotation commit and deleting the old key, or a record that started over.
        val run = run()
        enroll(run)
        val current = run.store.load()!!.alias!!
        run.keys.create(EnrollmentClient.ALIAS_PREFIX + "leftover")
        run.keys.create("another-feature-key")  // not ours: never touched
        run.client(transport = Transport { _, _, _, _ -> error("not asked") }).recover()
        assertEquals(setOf(current, "another-feature-key"), run.keys.keys.keys)
    }

    @Test
    fun `an unreadable record counts as not enrolled, and forget removes every identity key`() {
        val directory = Files.createTempDirectory("rynhult-enrollment").toFile()
        try {
            val file = directory.resolve("state.json")
            file.writeText("{\"origin\": \"https://acc.exa")  // torn by a crash
            val store = FileStateStore(file)
            assertNull(store.load())
            val keys = SoftwareKeys()
            keys.create(EnrollmentClient.ALIAS_PREFIX + "old")
            val client = EnrollmentClient(Fixtures.origin, Fixtures.PERSON, Fixtures.issuer,
                                          { _, _, _, _ -> error("unused") }, keys, store)
            assertFalse(client.state.enrolled)
            client.forget()
            assertTrue(keys.keys.isEmpty())
            assertFalse(store.load()!!.enrolled)
        } finally {
            directory.deleteRecursively()
        }
    }

    @Test
    fun `the identity is offered only to servers that accept the device CA`() {
        val ca = CredentialValidator.certificates(Fixtures.body("credential")["chain"]!!.jsonPrimitive.content)
            .single().subjectX500Principal
        assertTrue(ClientIdentity.offeredTo(ca, null))
        assertTrue(ClientIdentity.offeredTo(ca, emptyArray()))
        assertTrue(ClientIdentity.offeredTo(ca, arrayOf(javax.security.auth.x500.X500Principal("CN=Other CA"), ca)))
        assertFalse(ClientIdentity.offeredTo(ca, arrayOf(javax.security.auth.x500.X500Principal("CN=Other CA"))))
        assertFalse(ClientIdentity.offeredTo(null, arrayOf(ca)), "no stored chain: nothing to offer")
    }
}
