package {{package}}

import android.content.Context
import com.lagradost.cloudstream3.plugins.CloudstreamPlugin
import com.lagradost.cloudstream3.plugins.Plugin

@CloudstreamPlugin
class {{plugin_class}} : Plugin() {
    override fun load(context: Context) {
        registerMainAPI({{provider_class}}())
    }
}
