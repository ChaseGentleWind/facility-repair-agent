FROM node:20-alpine AS build
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm install
COPY . .
RUN npm run build
RUN sed -i 's|<script type="module" src="/src/repair-agent.ts"></script>|<script src="/dist/repair-agent.js"></script>|' index.html

FROM nginx:alpine
COPY --from=build /app/dist/ /usr/share/nginx/html/dist/
COPY --from=build /app/index.html /usr/share/nginx/html/
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
