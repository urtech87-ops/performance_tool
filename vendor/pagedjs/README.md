# Vendoring paged.js (optional)

`report_pdf.py` looks here for `paged.polyfill.min.js` before it falls back to
the cache dir or an `npm install`. Drop the file here on a machine that has no
npm or no network:

    npm pack pagedjs@0.4.3
    tar xzf pagedjs-0.4.3.tgz
    cp package/dist/paged.polyfill.min.js vendor/pagedjs/

paged.js is MIT-licensed (© 2018 Adam Hyde). The file is deliberately not
committed: it is ~500 KB of third-party build output, and most machines can
resolve it through the cache dir instead.
