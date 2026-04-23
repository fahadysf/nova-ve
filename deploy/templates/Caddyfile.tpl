{
    auto_https off
    admin off
}

:80 {
    encode zstd gzip
    root * __FRONTEND_ROOT__

    route {
        @api path /api/*
        handle @api {
            reverse_proxy 127.0.0.1:8000
        }

        @html5 path /html5*
        handle @html5 {
            reverse_proxy 127.0.0.1:8081
        }

        try_files {path} {path}/ /index.html
        file_server
    }
}
