{
    auto_https off
    admin off
}

:80 {
    encode zstd gzip
    root * __FRONTEND_ROOT__

    handle /api/* {
        reverse_proxy 127.0.0.1:8000
    }

    # Reserve /html5 for future Guacamole-backed console proxying.
    # handle /html5/* {
    #     reverse_proxy 127.0.0.1:8081
    # }

    try_files {path} {path}/ /index.html
    file_server
}
