package com.dykomenko.cameratransfer

import android.app.Activity
import android.content.ContentValues
import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import android.net.wifi.WifiNetworkSpecifier
import android.Manifest
import android.content.pm.PackageManager
import android.os.Build
import android.net.wifi.WifiManager
import android.nfc.NfcAdapter
import android.nfc.Tag
import android.nfc.tech.Ndef
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
import android.provider.MediaStore
import android.text.format.Formatter
import android.view.View
import android.view.ViewGroup
import android.view.WindowManager
import android.widget.BaseAdapter
import android.widget.Button
import android.widget.EditText
import android.widget.GridView
import android.widget.ImageView
import android.widget.ProgressBar
import android.widget.TextView
import android.content.Intent
import android.app.Dialog
import android.graphics.Color
import android.graphics.drawable.ColorDrawable
import android.view.Gravity
import android.view.Window
import com.chaquo.python.PyObject
import com.chaquo.python.Python
import org.json.JSONObject
import java.io.File
import java.net.Inet4Address
import java.net.NetworkInterface
import java.util.concurrent.Executors
import java.util.concurrent.Future

/** Wi-Fi details of the camera's own network, from its NFC tag. */
data class CameraWifi(val ssid: String, val password: String, val model: String)

/**
 * Sony NFC record "application/x-sony-pmm": a 5-byte header, then entries of
 * u16 tag + u16 length: 0x1000 "SSID:model", 0x1001 password (big-endian).
 */
fun parseSonyNfc(payload: ByteArray): CameraWifi? {
    var ssidAndModel: String? = null
    var password: String? = null
    var i = 5
    while (i + 4 <= payload.size) {
        val tag = ((payload[i].toInt() and 0xff) shl 8) or (payload[i + 1].toInt() and 0xff)
        val length = ((payload[i + 2].toInt() and 0xff) shl 8) or (payload[i + 3].toInt() and 0xff)
        if (i + 4 + length > payload.size) break
        val value = String(payload, i + 4, length, Charsets.UTF_8)
        when (tag) {
            0x1000 -> ssidAndModel = value
            0x1001 -> password = value
        }
        i += 4 + length
    }
    // The whole value is the network name: an a6500 broadcasts
    // "DIRECT-dKE1:ILCE-6500", model included.
    val ssid = ssidAndModel ?: return null
    if (!ssid.startsWith("DIRECT-") || password.isNullOrEmpty()) return null
    return CameraWifi(ssid, password, ssid.substringAfter(':', ""))
}

/**
 * Wi-Fi details from the QR code on the camera's "Send to Smartphone"
 * screen.  Sony uses "W01:S:<ssid>;P:<password>;C:<model>;M:<mac>;"; the
 * standard "WIFI:S:<ssid>;T:WPA;P:<password>;;" is accepted too.
 */
fun parseCameraQr(text: String): CameraWifi? {
    val body = text.trim().substringAfter("WIFI:", text.trim())
    val ssid = Regex("""(?:^|[;:])S:([^;]+)""").find(body)?.groupValues?.get(1) ?: return null
    val password = Regex("""(?:^|;)P:([^;]+)""").find(body)?.groupValues?.get(1) ?: return null
    val model = Regex("""(?:^|;)C:([^;]+)""").find(body)?.groupValues?.get(1) ?: ""
    if (ssid.isEmpty() || password.isEmpty()) return null
    // Sony's code carries only the part between "DIRECT-" and the model (seen
    // on an a6500: S:dKE1;C:ILCE-6500 for the network DIRECT-dKE1:ILCE-6500).
    val sony = text.trim().startsWith("W01:")
    return CameraWifi(if (sony) cameraSsid(ssid, model) else ssid, password, model)
}

/** Sony camera networks are named "DIRECT-<id>:<model>". */
fun cameraSsid(ssid: String, model: String): String {
    var name = if (ssid.startsWith("DIRECT-")) ssid else "DIRECT-$ssid"
    if (':' !in name && model.isNotEmpty()) name += ":$model"
    return name
}

data class CameraFile(
    val index: Int,
    val name: String,
    val size: Long,
    val captured: String,
    val raw: Boolean,
    val video: Boolean,
)

/** Called from Python while a file is being copied. */
class ProgressListener(private val onProgress: (Long, Long) -> Unit) {
    fun onProgress(done: Long, total: Long) = onProgress.invoke(done, total)
}

class MainActivity : Activity() {
    companion object {
        private const val QR_REQUEST = 4711
        // Sent by the NFC stand-in app (package com.sony.playmemories.mobile).
        private const val ACTION_CAMERA_NFC = "com.dykomenko.cameratransfer.CAMERA_NFC"
        private const val EXTRA_PMM = "sony_pmm"
    }

    // Every call into Python runs on this one thread, in order.
    private val worker = Executors.newSingleThreadExecutor()
    private val main = Handler(Looper.getMainLooper())
    private lateinit var bridge: PyObject

    /** How the phone talks to the camera; chosen once in the settings. */
    private enum class Mode(val key: String) { PC("pc"), PHONE("phone") }

    private lateinit var status: TextView
    private lateinit var home: View
    private lateinit var sessionView: View
    private lateinit var modeChip: TextView
    private lateinit var homeTitle: TextView
    private lateinit var homeHint: TextView
    private lateinit var primary: Button
    private lateinit var secondary: Button
    private lateinit var hotspotButton: Button
    private lateinit var cameraTitle: TextView
    private lateinit var cameraSub: TextView
    private lateinit var disconnectButton: Button
    private var cameraNetworkCallback: ConnectivityManager.NetworkCallback? = null
    private var direct = false
    private lateinit var progress: ProgressBar
    private lateinit var grid: GridView
    private lateinit var selection: TextView
    private lateinit var selectAll: Button
    private lateinit var downloadButton: Button

    private var files: List<CameraFile> = emptyList()
    private val selected = linkedSetOf<Int>()
    private val thumbnails = HashMap<Int, Bitmap>()
    private var onPhone: Set<String> = emptySet()  // "name|size" already in the gallery
    private var connected = false
    private var busy = false
    private var thumbnailJob: Future<*>? = null
    private var multicastLock: WifiManager.MulticastLock? = null
    private var wakeLock: PowerManager.WakeLock? = null

    private val adapter = object : BaseAdapter() {
        override fun getCount() = files.size
        override fun getItem(position: Int) = files[position]
        override fun getItemId(position: Int) = position.toLong()
        override fun getView(position: Int, convertView: View?, parent: ViewGroup): View {
            val view = convertView ?: layoutInflater.inflate(R.layout.item_photo, parent, false)
            val file = files[position]
            view.findViewById<ImageView>(R.id.thumb).setImageBitmap(thumbnails[file.index])
            view.findViewById<TextView>(R.id.name).text = file.name
            val badge = view.findViewById<TextView>(R.id.badge)
            val kind = when {
                file.raw -> "RAW"
                file.video -> "VIDEO"
                else -> file.name.substringAfterLast('.', "").uppercase()
            }
            badge.text = if (isOnPhone(file)) "$kind · ${getString(R.string.on_phone)}" else kind
            val isSelected = file.index in selected
            view.findViewById<View>(R.id.check).visibility = if (isSelected) View.VISIBLE else View.GONE
            view.findViewById<View>(R.id.frame).visibility = if (isSelected) View.VISIBLE else View.GONE
            view.alpha = if (isOnPhone(file) && !isSelected) 0.55f else 1f
            return view
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        bridge = Python.getInstance().getModule("android_bridge")
        status = findViewById(R.id.status)
        home = findViewById(R.id.home)
        sessionView = findViewById(R.id.session)
        modeChip = findViewById(R.id.mode_chip)
        homeTitle = findViewById(R.id.home_title)
        homeHint = findViewById(R.id.home_hint)
        primary = findViewById(R.id.primary)
        secondary = findViewById(R.id.secondary)
        hotspotButton = findViewById(R.id.hotspot)
        cameraTitle = findViewById(R.id.camera_title)
        cameraSub = findViewById(R.id.camera_sub)
        disconnectButton = findViewById(R.id.disconnect)
        progress = findViewById(R.id.progress)
        grid = findViewById(R.id.grid)
        selection = findViewById(R.id.selection)
        selectAll = findViewById(R.id.select_all)
        downloadButton = findViewById(R.id.download)

        findViewById<View>(R.id.settings).setOnClickListener { showSettings(false) }
        modeChip.setOnClickListener { showSettings(false) }
        primary.setOnClickListener {
            if (busy || connected) return@setOnClickListener
            if (mode() == Mode.PHONE) {
                val saved = savedCameraWifi()
                if (saved != null) connectDirect(saved) else scanQr()
            } else {
                connect()
            }
        }
        secondary.setOnClickListener { if (!busy && !connected) scanQr() }
        hotspotButton.setOnClickListener { openHotspotSettings() }
        disconnectButton.setOnClickListener { if (!busy) disconnect() }

        grid.adapter = adapter
        grid.setOnItemClickListener { _, _, position, _ ->
            if (busy) return@setOnItemClickListener
            val index = files[position].index
            if (!selected.add(index)) selected.remove(index)
            refreshSelection()
        }
        selectAll.setOnClickListener {
            if (busy) return@setOnClickListener
            if (selected.size == files.size) selected.clear() else files.forEach { selected.add(it.index) }
            refreshSelection()
        }
        downloadButton.setOnClickListener { downloadSelected() }
        refreshSelection()
        handleForwardedNfc(intent)
        if (mode() == null) showSettings(true)
    }

    private fun prefs() = getPreferences(Context.MODE_PRIVATE)

    private fun mode(): Mode? = Mode.values().firstOrNull { it.key == prefs().getString("mode", null) }

    private fun scanQr() {
        if (busy || connected) return
        @Suppress("DEPRECATION")
        startActivityForResult(Intent(this, QrScanActivity::class.java), QR_REQUEST)
    }

    /** One message line under the title; errors are tinted red. Empty text hides it. */
    private fun say(text: CharSequence?, error: Boolean = false) {
        if (text.isNullOrEmpty()) {
            status.visibility = View.GONE
            return
        }
        status.text = text
        status.setBackgroundResource(if (error) R.drawable.bg_banner_error else R.drawable.bg_banner)
        status.setTextColor(getColor(if (error) R.color.error_text else R.color.text))
        val h = (16 * resources.displayMetrics.density).toInt()
        val v = (12 * resources.displayMetrics.density).toInt()
        status.setPadding(h, v, h, v)
        status.visibility = View.VISIBLE
    }

    /** The mode chooser (a bottom sheet): picked once, then the app always works that way. */
    private fun showSettings(first: Boolean) {
        val dialog = Dialog(this)
        dialog.requestWindowFeature(Window.FEATURE_NO_TITLE)
        val view = layoutInflater.inflate(R.layout.dialog_settings, null)
        dialog.setContentView(view)
        dialog.window?.apply {
            setBackgroundDrawable(ColorDrawable(Color.TRANSPARENT))
            setLayout(WindowManager.LayoutParams.MATCH_PARENT, WindowManager.LayoutParams.WRAP_CONTENT)
            setGravity(Gravity.BOTTOM)
            setSoftInputMode(WindowManager.LayoutParams.SOFT_INPUT_ADJUST_RESIZE)
        }
        dialog.setCancelable(!first)
        val optionPc = view.findViewById<View>(R.id.option_pc)
        val optionPhone = view.findViewById<View>(R.id.option_phone)
        val radioPc = view.findViewById<View>(R.id.radio_pc)
        val radioPhone = view.findViewById<View>(R.id.radio_phone)
        val ipBlock = view.findViewById<View>(R.id.ip_block)
        val ip = view.findViewById<EditText>(R.id.camera_ip)
        val savedBlock = view.findViewById<View>(R.id.saved_block)
        val savedCamera = view.findViewById<TextView>(R.id.saved_camera)
        val done = view.findViewById<Button>(R.id.settings_done)
        ip.setText(prefs().getString("camera_ip", ""))
        var choice = mode()
        fun render() {
            optionPc.isSelected = choice == Mode.PC
            radioPc.isSelected = choice == Mode.PC
            optionPhone.isSelected = choice == Mode.PHONE
            radioPhone.isSelected = choice == Mode.PHONE
            ipBlock.visibility = if (choice == Mode.PC) View.VISIBLE else View.GONE
            done.isEnabled = choice != null
            val saved = savedCameraWifi()
            savedBlock.visibility = if (saved != null) View.VISIBLE else View.GONE
            savedCamera.text = getString(R.string.saved_camera, saved?.ssid ?: "")
        }
        optionPc.setOnClickListener { choice = Mode.PC; render() }
        optionPhone.setOnClickListener { choice = Mode.PHONE; render() }
        view.findViewById<Button>(R.id.forget).setOnClickListener {
            prefs().edit().remove("camera_ssid").remove("camera_password").remove("camera_model").apply()
            render()
        }
        done.setOnClickListener {
            prefs().edit()
                .putString("mode", choice?.key)
                .putString("camera_ip", ip.text.toString().trim())
                .apply()
            dialog.dismiss()
        }
        dialog.setOnDismissListener { say(null); refreshSelection() }
        render()
        dialog.show()
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handleForwardedNfc(intent)
    }

    /** The camera's tag, read while this app was closed, via the NFC stand-in app. */
    private fun handleForwardedNfc(intent: Intent?) {
        if (intent?.action != ACTION_CAMERA_NFC) return
        val payload = intent.getByteArrayExtra(EXTRA_PMM)
        intent.action = null  // not again after a rotation
        onCameraWifi(payload?.let { parseSonyNfc(it) })
    }

    /**
     * Apps cannot pick the hotspot's band, so open the phone's own hotspot
     * screen: there the band is set to 2.4 GHz (the camera has no 5 GHz) and
     * the hotspot switched on.
     */
    private fun openHotspotSettings() {
        val attempts = listOf(
            Intent("android.settings.TETHER_SETTINGS"),
            Intent(android.provider.Settings.ACTION_WIRELESS_SETTINGS),
            Intent(android.provider.Settings.ACTION_SETTINGS),
        )
        for (intent in attempts) {
            if (runCatching { startActivity(intent) }.isSuccess) return
        }
    }

    // ---------------------------------------------------------------- nfc
    override fun onResume() {
        super.onResume()
        NfcAdapter.getDefaultAdapter(this)?.enableReaderMode(
            this, { tag -> onNfcTag(tag) },
            NfcAdapter.FLAG_READER_NFC_A or NfcAdapter.FLAG_READER_NFC_B or NfcAdapter.FLAG_READER_NFC_F or
                NfcAdapter.FLAG_READER_NFC_V or NfcAdapter.FLAG_READER_NO_PLATFORM_SOUNDS, null)
    }

    override fun onPause() {
        NfcAdapter.getDefaultAdapter(this)?.disableReaderMode(this)
        super.onPause()
    }

    /** Log the shape of the camera's NFC tag (never its payload: it holds the password). */
    private fun onNfcTag(tag: Tag) {
        val lines = mutableListOf("NFC tag ${tag.id.toHex()} techs=${tag.techList.joinToString()}")
        val ndef = Ndef.get(tag)
        if (ndef != null) {
            runCatching {
                ndef.connect()
                val message = ndef.ndefMessage ?: ndef.cachedNdefMessage
                message?.records?.forEachIndexed { i, record ->
                    lines += "record $i tnf=${record.tnf} type=${String(record.type, Charsets.ISO_8859_1)} " +
                        "payload ${record.payload.size} bytes"
                }
                ndef.close()
            }.onFailure { lines += "read failed: $it" }
        } else {
            lines += "no NDEF"
        }
        lines.forEach { android.util.Log.i("CameraTransferNfc", it) }
        val wifi = runCatching {
            Ndef.get(tag)?.cachedNdefMessage?.records
                ?.firstOrNull { String(it.type, Charsets.ISO_8859_1) == "application/x-sony-pmm" }
                ?.let { parseSonyNfc(it.payload) }
        }.getOrNull()
        main.post { onCameraWifi(wifi) }
    }

    private fun onCameraWifi(wifi: CameraWifi?) {
        if (wifi == null) {
            say(getString(R.string.nfc_not_sony), error = true)
            return
        }
        rememberCameraWifi(wifi)
        // Touching the N mark only makes sense for the camera's own Wi-Fi; with
        // no mode chosen yet it picks that mode.
        if (mode() == null) prefs().edit().putString("mode", Mode.PHONE.key).apply()
        if (mode() != Mode.PHONE) {
            say(getString(R.string.nfc_wrong_mode))
            return
        }
        if (!busy && !connected) {
            say(getString(R.string.nfc_camera, wifi.model, wifi.ssid))
            connectDirect(wifi)
        }
    }

    @Deprecated("Activity result API of the plain framework Activity")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        if (requestCode != QR_REQUEST) {
            @Suppress("DEPRECATION")
            super.onActivityResult(requestCode, resultCode, data)
            return
        }
        val text = data?.getStringExtra(QrScanActivity.EXTRA_TEXT) ?: return
        // Log only the shape of the code, never the password.
        android.util.Log.i("CameraTransferQr", "field keys=" + text.split(';').map { it.substringBeforeLast(':', "") }
            .map { it.takeLast(1) })
        val wifi = parseCameraQr(text)
        if (wifi == null) {
            say(getString(R.string.qr_not_camera), error = true)
            return
        }
        rememberCameraWifi(wifi)
        say(getString(R.string.nfc_camera, wifi.model, wifi.ssid))
        connectDirect(wifi)
    }

    private fun rememberCameraWifi(wifi: CameraWifi) {
        getPreferences(Context.MODE_PRIVATE).edit()
            .putString("camera_ssid", wifi.ssid)
            .putString("camera_password", wifi.password)
            .putString("camera_model", wifi.model)
            .apply()
        refreshSelection()
    }

    private fun savedCameraWifi(): CameraWifi? {
        val prefs = getPreferences(Context.MODE_PRIVATE)
        // cameraSsid() also repairs a name saved by 0.4.x without "DIRECT-" or the model.
        val model = prefs.getString("camera_model", "") ?: ""
        val ssid = cameraSsid(prefs.getString("camera_ssid", null) ?: return null, model)
        val password = prefs.getString("camera_password", null) ?: return null
        return CameraWifi(ssid, password, model)
    }

    // ------------------------------------------------- camera's own Wi-Fi
    private var pendingDirect: CameraWifi? = null

    /**
     * Join the camera's own network ("Send to Smartphone") with a network
     * request: Android keeps it for this app although it has no internet,
     * which is where a plain Wi-Fi connection tends to fall apart.
     */
    private fun connectDirect(wifi: CameraWifi) {
        if (Build.VERSION.SDK_INT >= 33 &&
            checkSelfPermission(Manifest.permission.NEARBY_WIFI_DEVICES) != PackageManager.PERMISSION_GRANTED) {
            pendingDirect = wifi
            say(getString(R.string.permission_needed))
            requestPermissions(arrayOf(Manifest.permission.NEARBY_WIFI_DEVICES), 1)
            return
        }
        setBusy(true)
        say(getString(R.string.camera_wifi_joining, wifi.ssid))
        progress.isIndeterminate = true
        progress.visibility = View.VISIBLE
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        val connectivity = getSystemService(ConnectivityManager::class.java)
        releaseCameraNetwork()
        // Plain text log of what the camera answered, for troubleshooting.
        val diagPath = File(getExternalFilesDir(null) ?: filesDir, "camera-direct.log").absolutePath
        val specifier = WifiNetworkSpecifier.Builder()
            .setSsid(wifi.ssid)
            .setWpa2Passphrase(wifi.password)
            .build()
        val request = NetworkRequest.Builder()
            .addTransportType(NetworkCapabilities.TRANSPORT_WIFI)
            .removeCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
            .setNetworkSpecifier(specifier)
            .build()
        var started = false
        val callback = object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(network: Network) {
                android.util.Log.i("CameraTransfer", "camera network available: $network")
                connectivity.bindProcessToNetwork(network)
                if (started) return
                started = true
                worker.execute {
                    val result = runCatching { bridge.callAttr("connect_direct", 20, diagPath).toString() }
                    main.post {
                        progress.visibility = View.GONE
                        setBusy(false)
                        result.onSuccess { json -> showCamera(JSONObject(json)) }
                        result.onFailure { error ->
                            android.util.Log.w("CameraTransfer", "direct connect failed", error)
                            releaseCameraNetwork()
                            val reason = (error.message ?: error.toString()).lineSequence().first().take(200)
                            say(getString(R.string.camera_wifi_failed) + System.lineSeparator() + reason, error = true)
                        }
                    }
                }
            }

            override fun onUnavailable() {
                android.util.Log.w("CameraTransfer", "camera network unavailable (not found, refused or timed out)")
                main.post {
                    progress.visibility = View.GONE
                    setBusy(false)
                    releaseCameraNetwork()
                    say(getString(R.string.camera_wifi_failed), error = true)
                }
            }

            override fun onLost(network: Network) {
                android.util.Log.w("CameraTransfer", "camera network lost: $network")
                main.post {
                    if (connected && direct) {
                        connected = false
                        direct = false
                        say(getString(R.string.camera_wifi_failed), error = true)
                        releaseCameraNetwork()
                        refreshSelection()
                    }
                }
            }
        }
        cameraNetworkCallback = callback
        connectivity.requestNetwork(request, callback, 60_000)
    }

    private fun releaseCameraNetwork() {
        val connectivity = getSystemService(ConnectivityManager::class.java)
        cameraNetworkCallback?.let { runCatching { connectivity.unregisterNetworkCallback(it) } }
        cameraNetworkCallback = null
        runCatching { connectivity.bindProcessToNetwork(null) }
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        val wifi = pendingDirect ?: return
        pendingDirect = null
        if (grantResults.firstOrNull() == PackageManager.PERMISSION_GRANTED) connectDirect(wifi)
    }

    private fun ByteArray.toHex() = joinToString("") { "%02x".format(it) }

    override fun onDestroy() {
        thumbnailJob?.cancel(true)
        worker.execute { runCatching { bridge.callAttr("close") } }
        worker.shutdown()
        releaseNetwork()
        releaseCameraNetwork()
        super.onDestroy()
    }

    // ------------------------------------------------------------ network
    /**
     * Android sends traffic over mobile data when the Wi-Fi has no internet
     * (a camera, a hotspot without uplink).  Pin this app to the Wi-Fi so
     * the camera stays reachable, and let SSDP multicast replies in.
     */
    private fun prepareNetwork() {
        val connectivity = getSystemService(ConnectivityManager::class.java)
        val wifiNetwork: Network? = connectivity.allNetworks.firstOrNull {
            connectivity.getNetworkCapabilities(it)?.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) == true
        }
        // With the phone's hotspot on, the camera is on the hotspot interface;
        // binding to a Wi-Fi network would cut that path, so only bind when
        // the Wi-Fi is the phone's only local network.
        val wifiInterface = wifiNetwork?.let { connectivity.getLinkProperties(it)?.interfaceName }
        val otherLocal = localInterfaces().any { (name, _) -> name != wifiInterface }
        connectivity.bindProcessToNetwork(if (otherLocal) null else wifiNetwork)
        val wifi = applicationContext.getSystemService(WifiManager::class.java)
        multicastLock = wifi.createMulticastLock("camera-discovery").apply {
            setReferenceCounted(false)
            acquire()
        }
        val power = getSystemService(PowerManager::class.java)
        wakeLock = power.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "cameratransfer:session").apply {
            setReferenceCounted(false)
            acquire(60 * 60 * 1000L)
        }
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
    }

    private fun releaseNetwork() {
        runCatching { multicastLock?.release() }
        runCatching { wakeLock?.release() }
        multicastLock = null
        wakeLock = null
        window.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        runCatching { getSystemService(ConnectivityManager::class.java).bindProcessToNetwork(null) }
    }

    /** IPv4 addresses of this phone that can face the camera: Wi-Fi first, then hotspot. */
    private fun localAddresses(): List<String> = localInterfaces().map { it.second }.distinct()

    /** (interface name, IPv4 address) of local, non-cellular interfaces, Wi-Fi first. */
    private fun localInterfaces(): List<Pair<String, String>> {
        val cellular = listOf("rmnet", "ccmni", "pdp", "rev_rmnet", "v4-rmnet", "dummy", "tun", "lo")
        val found = mutableListOf<Pair<String, String>>()
        for (nic in NetworkInterface.getNetworkInterfaces().toList()) {
            if (!nic.isUp || nic.isLoopback || cellular.any { nic.name.startsWith(it) }) continue
            for (address in nic.inetAddresses.toList()) {
                if (address is Inet4Address && address.isSiteLocalAddress) {
                    found += nic.name to (address.hostAddress ?: continue)
                }
            }
        }
        return found.sortedBy { (name, _) -> if (name == "wlan0") 0 else 1 }
    }

    // ------------------------------------------------------------ actions
    private fun connect() {
        val ip = prefs().getString("camera_ip", "")?.trim() ?: ""
        prepareNetwork()
        setBusy(true)
        say(getString(R.string.searching))
        progress.isIndeterminate = true
        progress.visibility = View.VISIBLE
        val workDir = File(filesDir, "session").absolutePath
        val interfaces = localAddresses().joinToString(",")
        val lastIp = getPreferences(Context.MODE_PRIVATE).getString("last_camera_ip", "") ?: ""
        worker.execute {
            val result = runCatching {
                bridge.callAttr("connect", workDir, ip, interfaces, 5, lastIp).toString()
            }
            main.post {
                progress.visibility = View.GONE
                setBusy(false)
                result.onSuccess { json -> showCamera(JSONObject(json)) }
                result.onFailure { error ->
                    android.util.Log.w("CameraTransfer", "connect failed", error)
                    releaseNetwork()
                    say(getString(R.string.not_found), error = true)
                }
            }
        }
    }

    private fun showCamera(json: JSONObject) {
        connected = true
        direct = json.optBoolean("direct", false)
        json.optString("ip").takeIf { it.isNotEmpty() }?.let {
            getPreferences(Context.MODE_PRIVATE).edit().putString("last_camera_ip", it).apply()
        }
        val list = json.getJSONArray("files")
        files = (0 until list.length()).map {
            val item = list.getJSONObject(it)
            CameraFile(it, item.getString("name"), item.getLong("size"), item.getString("captured"),
                item.getBoolean("raw"), item.getBoolean("video"))
        }
        selected.clear()
        thumbnails.clear()
        onPhone = galleryContents()
        say(null)
        cameraTitle.text = json.getString("model")
        cameraSub.text = if (direct) getString(R.string.sub_direct, files.size)
        else getString(R.string.sub_pc, files.size,
            Formatter.formatShortFileSize(this, json.getLong("free")),
            Formatter.formatShortFileSize(this, json.getLong("capacity")))
        refreshSelection()
        loadThumbnails()
    }

    private fun loadThumbnails() {
        val pending = files.toList()
        thumbnailJob = worker.submit {
            for (file in pending) {
                if (Thread.currentThread().isInterrupted) return@submit
                val bytes = runCatching {
                    bridge.callAttr("thumbnail", file.index).toJava(ByteArray::class.java)
                }.getOrNull() ?: continue
                val bitmap = BitmapFactory.decodeByteArray(bytes, 0, bytes.size) ?: continue
                main.post {
                    thumbnails[file.index] = bitmap
                    adapter.notifyDataSetChanged()
                }
            }
        }
    }

    private fun disconnect() {
        thumbnailJob?.cancel(true)
        setBusy(true)
        worker.execute {
            runCatching { bridge.callAttr("close") }
            main.post {
                connected = false
                direct = false
                releaseCameraNetwork()
                files = emptyList()
                selected.clear()
                thumbnails.clear()
                releaseNetwork()
                setBusy(false)
                say(getString(R.string.disconnected))
                refreshSelection()
            }
        }
    }

    private fun downloadSelected() {
        val chosen = files.filter { it.index in selected }
        if (chosen.isEmpty() || busy) return
        thumbnailJob?.cancel(false)
        setBusy(true)
        progress.isIndeterminate = false
        progress.progress = 0
        progress.visibility = View.VISIBLE
        val totalBytes = chosen.sumOf { it.size }.coerceAtLeast(1)
        worker.execute {
            var doneBytes = 0L
            var saved = 0
            var error: Throwable? = null
            for ((number, file) in chosen.withIndex()) {
                val temp = File(cacheDir, file.name)
                val listener = ProgressListener { done, _ ->
                    val percent = ((doneBytes + done) * 100 / totalBytes).toInt()
                    main.post {
                        progress.progress = percent
                        say(getString(R.string.downloading, file.name, number + 1, chosen.size, percent))
                    }
                }
                var written = temp
                val result = runCatching {
                    written = File(bridge.callAttr("download", file.index, temp.absolutePath, listener).toString())
                    saveToGallery(written, file)
                }
                temp.delete()
                written.delete()
                if (result.isFailure) {
                    error = result.exceptionOrNull()
                    break
                }
                doneBytes += file.size
                saved++
                main.post {
                    selected.remove(file.index)
                    onPhone = onPhone + "${file.name}|${file.size}"
                    refreshSelection()
                }
            }
            val stillConnected = runCatching { bridge.callAttr("is_connected").toBoolean() }.getOrDefault(false)
            main.post {
                progress.visibility = View.GONE
                setBusy(false)
                val problem = error
                say(
                    when {
                        problem == null -> getString(R.string.done, saved)
                        !stillConnected -> getString(R.string.connection_lost)
                        else -> getString(R.string.download_failed, problem.message ?: problem.toString())
                    },
                    error = problem != null,
                )
                if (!stillConnected) {
                    connected = false
                    direct = false
                    releaseCameraNetwork()
                    releaseNetwork()
                    refreshSelection()
                }
            }
        }
    }

    // ------------------------------------------------------------ gallery
    private fun mimeType(name: String): String = when (name.substringAfterLast('.', "").lowercase()) {
        "jpg", "jpeg" -> "image/jpeg"
        "hif", "heif", "heic" -> "image/heif"
        "arw" -> "image/x-sony-arw"
        "dng" -> "image/x-adobe-dng"
        "mp4" -> "video/mp4"
        "mov" -> "video/quicktime"
        else -> "application/octet-stream"
    }

    private fun saveToGallery(source: File, file: CameraFile) {
        val video = file.video
        val collection = if (video) MediaStore.Video.Media.getContentUri(MediaStore.VOLUME_EXTERNAL_PRIMARY)
        else MediaStore.Images.Media.getContentUri(MediaStore.VOLUME_EXTERNAL_PRIMARY)
        val values = ContentValues().apply {
            put(MediaStore.MediaColumns.DISPLAY_NAME, file.name)
            put(MediaStore.MediaColumns.MIME_TYPE, mimeType(file.name))
            put(MediaStore.MediaColumns.RELATIVE_PATH, if (video) "Movies/Camera Transfer" else "Pictures/Camera Transfer")
            put(MediaStore.MediaColumns.IS_PENDING, 1)
        }
        val uri = contentResolver.insert(collection, values)
            ?: throw IllegalStateException("gallery refused ${file.name}")
        try {
            contentResolver.openOutputStream(uri)!!.use { out -> source.inputStream().use { it.copyTo(out) } }
            contentResolver.update(uri, ContentValues().apply { put(MediaStore.MediaColumns.IS_PENDING, 0) }, null, null)
        } catch (e: Exception) {
            contentResolver.delete(uri, null, null)
            throw e
        }
    }

    /** Files this app saved before, as "name|size", to mark them in the grid. */
    private fun galleryContents(): Set<String> {
        val found = HashSet<String>()
        val projection = arrayOf(MediaStore.MediaColumns.DISPLAY_NAME, MediaStore.MediaColumns.SIZE)
        for (collection in listOf(
            MediaStore.Images.Media.getContentUri(MediaStore.VOLUME_EXTERNAL_PRIMARY),
            MediaStore.Video.Media.getContentUri(MediaStore.VOLUME_EXTERNAL_PRIMARY),
        )) {
            runCatching {
                contentResolver.query(collection, projection,
                    "${MediaStore.MediaColumns.RELATIVE_PATH} LIKE ?", arrayOf("%Camera Transfer%"), null)?.use { c ->
                    while (c.moveToNext()) found += "${c.getString(0)}|${c.getLong(1)}"
                }
            }
        }
        return found
    }

    private fun isOnPhone(file: CameraFile) = "${file.name}|${file.size}" in onPhone

    // ----------------------------------------------------------------- ui
    private fun setBusy(value: Boolean) {
        busy = value
        refreshSelection()
    }

    /** Re-draw everything that depends on state: which screen, which buttons, the selection bar. */
    private fun refreshSelection() {
        home.visibility = if (connected) View.GONE else View.VISIBLE
        sessionView.visibility = if (connected) View.VISIBLE else View.GONE
        val phoneMode = mode() == Mode.PHONE
        val saved = savedCameraWifi()
        modeChip.text = getString(R.string.mode_chip, getString(if (phoneMode) R.string.mode_phone_name else R.string.mode_pc_name))
        homeTitle.setText(if (phoneMode) R.string.home_title_phone else R.string.home_title_pc)
        homeHint.setText(if (phoneMode) R.string.home_hint_phone else R.string.home_hint_pc)
        primary.setText(if (phoneMode && saved == null) R.string.scan_qr else R.string.connect)
        secondary.visibility = if (phoneMode && saved != null) View.VISIBLE else View.GONE
        secondary.setText(R.string.scan_qr_again)
        hotspotButton.visibility = if (phoneMode) View.GONE else View.VISIBLE
        primary.isEnabled = !busy
        secondary.isEnabled = !busy
        disconnectButton.isEnabled = !busy

        val bytes = files.filter { it.index in selected }.sumOf { it.size }
        selection.text = if (selected.isEmpty()) getString(R.string.nothing_selected)
        else getString(R.string.selected, selected.size, Formatter.formatShortFileSize(this, bytes))
        downloadButton.isEnabled = connected && !busy && selected.isNotEmpty()
        selectAll.isEnabled = connected && !busy && files.isNotEmpty()
        selectAll.text = getString(if (files.isNotEmpty() && selected.size == files.size) R.string.select_none else R.string.select_all)
        adapter.notifyDataSetChanged()
    }
}
