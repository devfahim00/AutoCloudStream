package com.redtube

import android.content.Context
import com.lagradost.cloudstream3.plugins.CloudstreamPlugin
import com.lagradost.cloudstream3.plugins.Plugin

@CloudstreamPlugin
class RedtubePlugin : Plugin() {
    override fun load(context: Context) {
        registerMainAPI(RedtubeProvider())
    }
}
