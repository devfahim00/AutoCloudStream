package com.kamababa

import android.content.Context
import com.lagradost.cloudstream3.plugins.CloudstreamPlugin
import com.lagradost.cloudstream3.plugins.Plugin

@CloudstreamPlugin
class KamababaPlugin : Plugin() {
    override fun load(context: Context) {
        registerMainAPI(KamababaProvider())
    }
}
