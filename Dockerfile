FROM nginx:1.27-alpine
ARG APP_COMMIT=local
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY index.html /usr/share/nginx/html/index.html
COPY favicon.svg /usr/share/nginx/html/favicon.svg
COPY favicon-32x32.png /usr/share/nginx/html/favicon-32x32.png
COPY apple-touch-icon.png /usr/share/nginx/html/apple-touch-icon.png
COPY icon-192.png /usr/share/nginx/html/icon-192.png
COPY site.webmanifest /usr/share/nginx/html/site.webmanifest
RUN sed -i "s/__APP_COMMIT__/${APP_COMMIT}/g" /usr/share/nginx/html/index.html
EXPOSE 80
