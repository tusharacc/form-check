#!/bin/sh
cd /Users/tusharsaurabh/Documents/Projects/Python/formcheck/renderer
exec python3 -m http.server "${PORT:-3333}"
