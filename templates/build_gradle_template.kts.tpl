version = 2

cloudstream {
    authors     = listOf("{{author}}")
    language    = "{{lang}}"
    description = "{{description}}"

    /**
     * Status int as the following:
     * 0: Down
     * 1: Ok
     * 2: Slow
     * 3: Beta only
    **/
    status  = {{status}}
    tvTypes = listOf({{tv_types_gradle}})
    iconUrl = "{{icon_url}}"
}
