FROM nginx:1.27-alpine
ARG APP_COMMIT=local
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY index.html /usr/share/nginx/html/index.html
COPY assets/ /usr/share/nginx/html/assets/
COPY favicon.svg /usr/share/nginx/html/favicon.svg
COPY og-image.svg /usr/share/nginx/html/og-image.svg
COPY og-image.png /usr/share/nginx/html/og-image.png
COPY favicon-32x32.png /usr/share/nginx/html/favicon-32x32.png
COPY apple-touch-icon.png /usr/share/nginx/html/apple-touch-icon.png
COPY icon-192.png /usr/share/nginx/html/icon-192.png
COPY site.webmanifest /usr/share/nginx/html/site.webmanifest
RUN sed -i "s/__APP_COMMIT__/${APP_COMMIT}/g" /usr/share/nginx/html/index.html /usr/share/nginx/html/assets/js/live.js
EXPOSE 80
