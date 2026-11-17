package com.dykomenko.cameratransfer

import android.app.Activity
import android.content.ContentValues
import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.wifi.WifiManager
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
import com.chaquo.python.PyObject
import com.chaquo.python.Python
import org.json.JSONObject
import java.io.File
import java.net.Inet4Address
import java.net.NetworkInterface
import java.util.concurrent.Executors
import java.util.concurrent.Future

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
    // Every call into Python runs on this one thread, in order.
    private val worker = Executors.newSingleThreadExecutor()
    private val main = Handler(Looper.getMainLooper())
    private lateinit var bridge: PyObject

    private lateinit var status: TextView
    private lateinit var cameraIp: EditText
    private lateinit var connectButton: Button
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
        cameraIp = findViewById(R.id.camera_ip)
        connectButton = findViewById(R.id.connect)
        progress = findViewById(R.id.progress)
        grid = findViewById(R.id.grid)
        selection = findViewById(R.id.selection)
        selectAll = findViewById(R.id.select_all)
        downloadButton = findViewById(R.id.download)

        cameraIp.setText(getPreferences(Context.MODE_PRIVATE).getString("camera_ip", ""))
        grid.adapter = adapter
        grid.setOnItemClickListener { _, _, position, _ ->
            if (busy) return@setOnItemClickListener
            val index = files[position].index
            if (!selected.add(index)) selected.remove(index)
            refreshSelection()
        }
        connectButton.setOnClickListener { if (connected) disconnect() else connect() }
        selectAll.setOnClickListener {
            if (busy) return@setOnClickListener
            if (selected.size == files.size) selected.clear() else files.forEach { selected.add(it.index) }
            refreshSelection()
        }
        downloadButton.setOnClickListener { downloadSelected() }
        refreshSelection()
    }

    override fun onDestroy() {
        thumbnailJob?.cancel(true)
        worker.execute { runCatching { bridge.callAttr("close") } }
        worker.shutdown()
        releaseNetwork()
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
        val ip = cameraIp.text.toString().trim()
        getPreferences(Context.MODE_PRIVATE).edit().putString("camera_ip", ip).apply()
        prepareNetwork()
        setBusy(true)
        status.text = getString(R.string.searching)
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
                    status.text = getString(R.string.not_found)
                }
            }
        }
    }

    private fun showCamera(json: JSONObject) {
        connected = true
        json.optString("ip").takeIf { it.isNotEmpty() }?.let {
            getPreferences(Context.MODE_PRIVATE).edit().putString("last_camera_ip", it).apply()
        }
        connectButton.text = getString(R.string.disconnect)
        val list = json.getJSONArray("files")
        files = (0 until list.length()).map {
            val item = list.getJSONObject(it)
            CameraFile(it, item.getString("name"), item.getLong("size"), item.getString("captured"),
                item.getBoolean("raw"), item.getBoolean("video"))
        }
        selected.clear()
        thumbnails.clear()
        onPhone = galleryContents()
        status.text = getString(R.string.connected, json.getString("model"), files.size,
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
                files = emptyList()
                selected.clear()
                thumbnails.clear()
                releaseNetwork()
                setBusy(false)
                connectButton.text = getString(R.string.connect)
                status.text = getString(R.string.disconnected)
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
                        status.text = getString(R.string.downloading, file.name, number + 1, chosen.size, percent)
                    }
                }
                val result = runCatching {
                    bridge.callAttr("download", file.index, temp.absolutePath, listener)
                    saveToGallery(temp, file)
                }
                temp.delete()
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
                status.text = when {
                    problem == null -> getString(R.string.done, saved)
                    !stillConnected -> getString(R.string.connection_lost)
                    else -> getString(R.string.download_failed, problem.message ?: problem.toString())
                }
                if (!stillConnected) {
                    connected = false
                    connectButton.text = getString(R.string.connect)
                    releaseNetwork()
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
        connectButton.isEnabled = !value
        cameraIp.isEnabled = !value && !connected
        refreshSelection()
    }

    private fun refreshSelection() {
        val bytes = files.filter { it.index in selected }.sumOf { it.size }
        selection.text = if (selected.isEmpty()) getString(R.string.nothing_selected)
        else getString(R.string.selected, selected.size, Formatter.formatShortFileSize(this, bytes))
        downloadButton.isEnabled = connected && !busy && selected.isNotEmpty()
        selectAll.isEnabled = connected && !busy && files.isNotEmpty()
        selectAll.text = getString(if (files.isNotEmpty() && selected.size == files.size) R.string.select_none else R.string.select_all)
        adapter.notifyDataSetChanged()
    }
}
