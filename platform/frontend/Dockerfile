FROM node:22-alpine

WORKDIR /app
COPY package.json angular.json eslint.config.js proxy.conf.json tsconfig.json tsconfig.app.json .prettierrc ./
RUN npm install
COPY src ./src

EXPOSE 4200
CMD ["npm", "start"]
