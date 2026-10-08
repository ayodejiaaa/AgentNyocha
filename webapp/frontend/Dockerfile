# The review screen (Next.js). NEXT_PUBLIC_API_URL is read when the app is BUILT,
# not when it runs: pass the API's public address as a build argument, and rebuild
# if it ever changes.
#
#   docker build --build-arg NEXT_PUBLIC_API_URL=https://api.example.com -t review-web webapp/frontend
FROM node:20-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
ARG NEXT_PUBLIC_API_URL
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL
RUN npm run build

FROM node:20-alpine
WORKDIR /app
ENV NODE_ENV=production
COPY package.json package-lock.json ./
RUN npm ci --omit=dev
COPY --from=build /app/.next ./.next
COPY --from=build /app/next.config.js ./
USER node
EXPOSE 3000
CMD ["node_modules/.bin/next", "start", "-p", "3000"]
