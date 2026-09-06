# Base image pinned by DIGEST, not just tag: a tag is mutable, so an unpinned
# build is not reproducible and silently picks up whatever the tag points at
# today. The tag is kept alongside the digest for readability. Refresh with:
#   docker buildx imagetools inspect node:22-alpine
FROM node:22-alpine@sha256:c610fcdfb1d5b4740dd70c284ed3cb16bb857e0f7166196e36a5501df7a3aa32

WORKDIR /app

COPY package*.json ./
RUN npm install

COPY . .

EXPOSE 5174

CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]
