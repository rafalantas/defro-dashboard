FROM nginx:1.27-alpine
ARG APP_COMMIT=local
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY index.html /usr/share/nginx/html/index.html
RUN sed -i "s/__APP_COMMIT__/${APP_COMMIT}/g" /usr/share/nginx/html/index.html
EXPOSE 80
