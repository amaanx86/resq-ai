# Stage 1: Build React 19 Frontend
FROM node:22-alpine AS builder

WORKDIR /app

COPY package*.json ./
RUN npm ci

COPY . .

# Portal auth (UI-2). Vite inlines VITE_* at build time, so pass them as build args, e.g.
#   docker build --build-arg VITE_AUTH_ENABLED=true --build-arg VITE_COGNITO_DOMAIN=... .
# Defaults keep today's behaviour: no sign-in, local admin.
ARG VITE_AUTH_ENABLED=false
ARG VITE_COGNITO_DOMAIN=
ARG VITE_COGNITO_CLIENT_ID=
ARG VITE_COGNITO_REDIRECT_URI=
ARG VITE_COGNITO_LOGOUT_URI=
ARG VITE_AWS_REGION=us-east-1
ARG VITE_COGNITO_USER_POOL_ID=
ENV VITE_AUTH_ENABLED=$VITE_AUTH_ENABLED     VITE_COGNITO_DOMAIN=$VITE_COGNITO_DOMAIN     VITE_COGNITO_CLIENT_ID=$VITE_COGNITO_CLIENT_ID     VITE_COGNITO_REDIRECT_URI=$VITE_COGNITO_REDIRECT_URI     VITE_COGNITO_LOGOUT_URI=$VITE_COGNITO_LOGOUT_URI     VITE_AWS_REGION=$VITE_AWS_REGION     VITE_COGNITO_USER_POOL_ID=$VITE_COGNITO_USER_POOL_ID

RUN npm run build

# Stage 2: Serve via Nginx Reverse Proxy
FROM nginx:alpine

COPY --from=builder /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD wget -qO- http://localhost/ || exit 1

CMD ["nginx", "-g", "daemon off;"]
