# CMED's test site, for the local stack. Next.js, built once and served.
FROM node:20-alpine AS build
WORKDIR /app
COPY package*.json ./
RUN npm ci --no-audit --no-fund
COPY . .
# Next.js writes NEXT_PUBLIC_* into the browser bundle while it builds, so
# these have to arrive here rather than at `docker run`. The clinic code
# matters most: the page sends it with every visit, and a visit described
# under a clinic the recorder does not belong to is never confirmed.
ARG NEXT_PUBLIC_HOSPITAL_ID=CMED-LOCAL-01
ARG NEXT_PUBLIC_BACKEND_URL=http://localhost:6060
ARG NEXT_PUBLIC_RECORDER_WS=ws://localhost:5050/ws
ENV NEXT_PUBLIC_HOSPITAL_ID=$NEXT_PUBLIC_HOSPITAL_ID
ENV NEXT_PUBLIC_BACKEND_URL=$NEXT_PUBLIC_BACKEND_URL
ENV NEXT_PUBLIC_RECORDER_WS=$NEXT_PUBLIC_RECORDER_WS
RUN npm run build

FROM node:20-alpine
WORKDIR /app
ENV NODE_ENV=production
COPY --from=build /app/.next ./.next
COPY --from=build /app/node_modules ./node_modules
COPY --from=build /app/package.json ./package.json
# No public/ folder in this project; Next.js does not require one.
EXPOSE 3000
CMD ["npm", "run", "start"]
