package com.dykomenko.cameratransfer.nfcstub

import android.app.Activity
import android.content.ComponentName
import android.content.Intent
import android.nfc.NdefMessage
import android.nfc.NfcAdapter
import android.os.Bundle

/** Receives the camera's NFC tag and passes its Wi-Fi record to Camera Transfer. */
class NfcForwardActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val payload = sonyRecord(intent)
        val forward = Intent(ACTION_CAMERA_NFC)
            .setComponent(ComponentName(TARGET_PACKAGE, "$TARGET_PACKAGE.MainActivity"))
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP)
        if (payload != null) forward.putExtra(EXTRA_PMM, payload)
        runCatching { startActivity(forward) }
        finish()
    }

    private fun sonyRecord(intent: Intent): ByteArray? {
        @Suppress("DEPRECATION")
        val messages = intent.getParcelableArrayExtra(NfcAdapter.EXTRA_NDEF_MESSAGES) ?: return null
        return messages.filterIsInstance<NdefMessage>()
            .flatMap { it.records.asList() }
            .firstOrNull { String(it.type, Charsets.ISO_8859_1) == "application/x-sony-pmm" }
            ?.payload
    }

    companion object {
        const val TARGET_PACKAGE = "com.dykomenko.cameratransfer"
        const val ACTION_CAMERA_NFC = "com.dykomenko.cameratransfer.CAMERA_NFC"
        const val EXTRA_PMM = "sony_pmm"
    }
}
