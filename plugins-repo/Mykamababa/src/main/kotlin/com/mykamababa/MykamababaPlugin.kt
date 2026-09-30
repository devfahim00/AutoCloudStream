package com.mykamababa

import android.content.Context
import com.lagradost.cloudstream3.plugins.CloudstreamPlugin
import com.lagradost.cloudstream3.plugins.Plugin

@CloudstreamPlugin
class MykamababaPlugin : Plugin() {
    override fun load(context: Context) {
        registerMainAPI(MykamababaProvider())
    }
}
